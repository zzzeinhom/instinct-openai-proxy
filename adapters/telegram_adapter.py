"""Telegram MTProto adapter with multi-message silence-window and SSE streaming."""

import asyncio
from collections.abc import AsyncIterator
import logging
from typing import Any

from telethon import TelegramClient, events

from adapters.base_adapter import BaseAdapter
from config import Settings, settings as default_settings

logger = logging.getLogger(__name__)

_TELEGRAM_CHUNK_LIMIT = 4000


def _chunk_text(text: str, max_len: int = _TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split text into chunks within Telegram's message length limit."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramAdapter(BaseAdapter):
    """Telethon-based adapter for interacting with a stateful Telegram bot."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or default_settings
        self._client: TelegramClient | None = None
        self._bot_entity: Any = None

    async def startup(self) -> None:
        """Connect Telethon using the saved session without interactive prompts."""
        self._settings.validate_telegram_config(require_phone=False)

        # Instantiate inside the running event loop (required on Python 3.14).
        client = TelegramClient(
            self._settings.tg_session_path(),
            self._settings.TG_API_ID,
            self._settings.TG_API_HASH,
        )
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                "Telegram session is not authorized. Run 'python auth_telegram.py --qr' first."
            )

        self._client = client
        self._bot_entity = await client.get_entity(
            self._settings.INSTINCT_TG_USERNAME
        )
        logger.info(
            "TelegramAdapter connected and resolved target %s",
            self._settings.INSTINCT_TG_USERNAME,
        )

    async def shutdown(self) -> None:
        """Disconnect the Telethon client if active."""
        if self._client is not None and self._client.is_connected():
            await self._client.disconnect()
            logger.info("TelegramAdapter disconnected.")
        self._client = None
        self._bot_entity = None

    async def _iter_reply_snapshots(
        self, prompt: str
    ) -> AsyncIterator[dict[int, str]]:
        """Send prompt and yield ordered tracked_messages snapshots until settled."""
        if self._client is None or not self._client.is_connected():
            raise RuntimeError("TelegramAdapter is not connected.")
        if self._bot_entity is None:
            raise RuntimeError("Target Telegram bot entity is not resolved.")
        if not await self._client.is_user_authorized():
            raise RuntimeError(
                "Telegram session is no longer authorized. Re-run 'python auth_telegram.py --qr'."
            )

        chunks = _chunk_text(prompt, max_len=_TELEGRAM_CHUNK_LIMIT)

        loop = asyncio.get_running_loop()
        prompt_msg_id: int | None = None
        pending_events: list[tuple[int, str, float]] = []
        tracked_messages: dict[int, str] = {}
        last_activity_at: float | None = None
        activity_event = asyncio.Event()

        async def _on_new_or_edit(event: Any) -> None:
            nonlocal last_activity_at
            msg = getattr(event, "message", None)
            if msg is None or getattr(msg, "out", False):
                return
            msg_id = msg.id
            text = msg.raw_text or msg.message or ""
            now = loop.time()
            if prompt_msg_id is None:
                pending_events.append((msg_id, text, now))
                return
            if msg_id > prompt_msg_id:
                tracked_messages[msg_id] = text
                last_activity_at = now
                activity_event.set()

        async def _on_deleted(event: Any) -> None:
            nonlocal last_activity_at
            deleted_ids = getattr(event, "deleted_ids", None) or []
            if prompt_msg_id is None:
                deleted_set = set(deleted_ids)
                pending_events[:] = [
                    item for item in pending_events if item[0] not in deleted_set
                ]
                return
            changed = False
            for deleted_id in deleted_ids:
                if deleted_id in tracked_messages:
                    del tracked_messages[deleted_id]
                    changed = True
            if changed:
                last_activity_at = loop.time()
                activity_event.set()

        new_msg_builder = events.NewMessage(
            chats=self._bot_entity, incoming=True
        )
        edit_msg_builder = events.MessageEdited(
            chats=self._bot_entity, incoming=True
        )
        del_msg_builder = events.MessageDeleted()

        self._client.add_event_handler(_on_new_or_edit, new_msg_builder)
        self._client.add_event_handler(_on_new_or_edit, edit_msg_builder)
        self._client.add_event_handler(_on_deleted, del_msg_builder)

        try:
            last_sent = None
            for chunk in chunks:
                last_sent = await self._client.send_message(
                    self._bot_entity, chunk
                )
            if last_sent is None:
                raise ValueError("Cannot send an empty prompt to Telegram.")

            prompt_msg_id = last_sent.id
            for msg_id, text, event_time in pending_events:
                if msg_id > prompt_msg_id:
                    tracked_messages[msg_id] = text
                    last_activity_at = event_time
                    activity_event.set()
            pending_events.clear()

            deadline = loop.time() + self._settings.RESPONSE_TIMEOUT_SECONDS
            emitted_any = False

            while True:
                activity_event.clear()
                has_non_empty = any(
                    text.strip() for text in tracked_messages.values()
                )
                if has_non_empty:
                    emitted_any = True
                    yield dict(tracked_messages)

                now = loop.time()
                remaining = deadline - now
                if remaining <= 0:
                    break

                if not has_non_empty:
                    try:
                        await asyncio.wait_for(
                            activity_event.wait(), timeout=remaining
                        )
                    except asyncio.TimeoutError:
                        break
                else:
                    anchor = last_activity_at if last_activity_at is not None else now
                    settle_remaining = self._settings.REPLY_SETTLE_SECONDS - (
                        now - anchor
                    )
                    if settle_remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(
                            activity_event.wait(),
                            timeout=min(settle_remaining, remaining),
                        )
                    except asyncio.TimeoutError:
                        break

            if not emitted_any:
                raise TimeoutError(
                    "Timed out waiting for a response from the Telegram bot."
                )
        finally:
            self._client.remove_event_handler(_on_new_or_edit, new_msg_builder)
            self._client.remove_event_handler(_on_new_or_edit, edit_msg_builder)
            self._client.remove_event_handler(_on_deleted, del_msg_builder)

    async def stream_message(self, prompt: str) -> AsyncIterator[str]:
        """Yield incremental text deltas as Telegram messages/edits arrive."""
        emitted_text = ""
        async for snapshot in self._iter_reply_snapshots(prompt=prompt):
            combined = "\n\n".join(
                text for _, text in sorted(snapshot.items()) if text.strip()
            )
            if not emitted_text:
                combined = combined.lstrip()
            if len(combined) > len(emitted_text):
                delta = combined[len(emitted_text) :]
                emitted_text = combined
                if delta:
                    yield delta

        if not emitted_text:
            raise TimeoutError(
                "Timed out waiting for a response from the Telegram bot."
            )

    async def send_message(self, prompt: str) -> str:
        """Send prompt and accumulate multi-message replies until settled."""
        latest_snapshot: dict[int, str] = {}
        async for snapshot in self._iter_reply_snapshots(prompt=prompt):
            latest_snapshot = snapshot

        final_parts = [
            text.strip()
            for _, text in sorted(latest_snapshot.items())
            if text.strip()
        ]
        if not final_parts:
            raise TimeoutError(
                "Timed out waiting for a response from the Telegram bot."
            )
        return "\n\n".join(final_parts)
