"""WhatsApp (WAHA) REST and webhook adapter with alias resolution and SSE streaming."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import logging
import re
from typing import Any

import httpx

from adapters.base_adapter import BaseAdapter
from config import Settings, settings as default_settings

logger = logging.getLogger(__name__)

_DIRECT_WA_DOMAINS = {"", "c.us", "lid", "s.whatsapp.net"}


def _split_wa_id(chat_id: str) -> tuple[str, str]:
    """Split a WhatsApp JID/phone into (normalized_local, lowercase_domain)."""
    cleaned = chat_id.strip()
    if "@" in cleaned:
        local, domain = cleaned.split("@", 1)
        return local.lstrip("+"), domain.lower()
    return cleaned.lstrip("+"), ""


def _digits_only(jid_or_phone: str) -> str:
    """Extract numeric digits from the local part of a JID or phone string."""
    local, _ = _split_wa_id(jid_or_phone)
    return re.sub(r"\D", "", local)


def _chat_ids_match(incoming: str, target: str) -> bool:
    """Safely compare WAHA chat IDs across @c.us, @lid, and @s.whatsapp.net suffixes."""
    inc_local, inc_domain = _split_wa_id(incoming)
    tgt_local, tgt_domain = _split_wa_id(target)
    if not inc_local or not tgt_local:
        return False
    if inc_domain in _DIRECT_WA_DOMAINS and tgt_domain in _DIRECT_WA_DOMAINS:
        return inc_local == tgt_local
    return inc_local == tgt_local and inc_domain == tgt_domain


def _format_send_chat_id(target: str) -> str:
    """Ensure target WhatsApp recipient includes a chat suffix for WAHA /api/sendText."""
    cleaned = target.strip()
    if "@" in cleaned:
        return cleaned
    return f"{cleaned.lstrip('+')}@c.us"


def _extract_serialized_jid(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        val = raw.get("_serialized")
        if isinstance(val, str):
            return val.strip()
    return ""


def _extract_jids_from_chat(chat: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Extract all JIDs and @lid JIDs from top-level and nested _chat WAHA records."""
    all_jids: set[str] = set()
    lid_jids: set[str] = set()

    sources: list[dict[str, Any]] = [chat]
    nested_chat = chat.get("_chat")
    if isinstance(nested_chat, dict):
        sources.append(nested_chat)

    for src in sources:
        for field in ("id", "pnJid", "accountLid", "lid"):
            jid = _extract_serialized_jid(src.get(field))
            if not jid:
                continue
            normalized = jid.lower()
            all_jids.add(normalized)
            if field in {"accountLid", "lid"} or normalized.endswith("@lid"):
                lid_jids.add(normalized)

    return all_jids, lid_jids


def _coerce_chat_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("chats", "data", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict)]
    return []


def build_waha_webhooks(
    webhook_url: str, webhook_secret: str = ""
) -> list[dict[str, Any]]:
    """Build the WAHA webhook configuration payload."""
    entry: dict[str, Any] = {
        "url": webhook_url.strip(),
        "events": ["message"],
    }
    if webhook_secret:
        entry["customHeaders"] = [
            {"name": "X-Webhook-Secret", "value": webhook_secret}
        ]
    return [entry]


def _normalize_webhook_entry(
    entry: dict[str, Any],
) -> tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Normalize a WAHA webhook dict for stable comparison across extra defaults."""
    url = str(entry.get("url") or "").strip()
    events = tuple(sorted(str(e) for e in (entry.get("events") or [])))
    raw_headers = entry.get("customHeaders") or []
    headers = tuple(
        sorted(
            (str(h.get("name") or ""), str(h.get("value") or ""))
            for h in raw_headers
            if isinstance(h, dict)
        )
    )
    return (url, events, headers)


def _webhooks_match(
    existing: Any, desired: list[dict[str, Any]]
) -> bool:
    """Return True if existing WAHA webhooks match the desired URL, events, and headers."""
    if not isinstance(existing, list) or len(existing) != len(desired):
        return False
    existing_norm = sorted(
        _normalize_webhook_entry(item)
        for item in existing
        if isinstance(item, dict)
    )
    desired_norm = sorted(_normalize_webhook_entry(item) for item in desired)
    return existing_norm == desired_norm


@dataclass
class _PendingBuffer:
    messages: list[str]
    seen_ids: set[str]
    notify_event: asyncio.Event
    last_activity_at: float | None = None
    alias_synced: bool = False


class WhatsAppAdapter(BaseAdapter):
    """WAHA REST + webhook adapter for stateful WhatsApp bot interactions."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or default_settings
        self._client: httpx.AsyncClient | None = None
        self._pending_buffer: _PendingBuffer | None = None
        self._allowed_sender_ids: set[str] = self._initial_allowed_senders()

    def _initial_allowed_senders(self) -> set[str]:
        target = self._settings.INSTINCT_WA_NUMBER.strip().lower()
        return {target} if target else set()

    def _is_allowed_sender(self, sender: str) -> bool:
        cleaned = sender.strip().lower()
        if not cleaned:
            return False
        if cleaned in self._allowed_sender_ids:
            return True
        return any(
            _chat_ids_match(cleaned, allowed)
            for allowed in self._allowed_sender_ids
        )

    async def _sync_contact_aliases(self) -> None:
        """Resolve @c.us phone JIDs to user-scoped @lid aliases from WAHA."""
        if self._client is None:
            return

        target_digits = _digits_only(self._settings.INSTINCT_WA_NUMBER)
        session = self._settings.WAHA_SESSION

        if target_digits:
            lid_resp = await self._client.get(
                f"/api/{session}/lids/pn/{target_digits}"
            )
            if lid_resp.status_code == 200:
                lid_data = lid_resp.json()
                lid_val = (
                    lid_data.get("lid") if isinstance(lid_data, dict) else None
                )
                if isinstance(lid_val, str) and lid_val.strip():
                    normalized_lid = lid_val.strip().lower()
                    if normalized_lid not in self._allowed_sender_ids:
                        self._allowed_sender_ids.add(normalized_lid)
                        logger.info(
                            "Resolved WhatsApp @lid via /lids/pn/%s: %s",
                            target_digits,
                            normalized_lid,
                        )
                    return

        for endpoint in (
            f"/api/{session}/chats/overview",
            f"/api/{session}/chats",
        ):
            resp = await self._client.get(endpoint, params={"limit": 50})
            if resp.status_code != 200:
                continue
            chats = _coerce_chat_list(resp.json())
            added = False
            for chat in chats:
                all_jids, lid_jids = _extract_jids_from_chat(chat)
                if not all_jids:
                    continue

                matches_target = any(
                    self._is_allowed_sender(jid)
                    or (
                        bool(target_digits)
                        and _digits_only(jid) == target_digits
                    )
                    for jid in all_jids
                )
                if not matches_target:
                    continue

                for alias in lid_jids | all_jids:
                    if alias not in self._allowed_sender_ids:
                        self._allowed_sender_ids.add(alias)
                        added = True

            if added:
                logger.info(
                    "Resolved WhatsApp contact aliases for %s: %s",
                    self._settings.INSTINCT_WA_NUMBER,
                    sorted(self._allowed_sender_ids),
                )
            if chats:
                return

    async def startup(self) -> None:
        """Validate WAHA config, init httpx.AsyncClient, provision, and sync aliases."""
        self._settings.validate_whatsapp_config()
        self._allowed_sender_ids = self._initial_allowed_senders()

        headers: dict[str, str] = {}
        if self._settings.WAHA_API_KEY:
            headers["X-Api-Key"] = self._settings.WAHA_API_KEY

        self._client = httpx.AsyncClient(
            base_url=self._settings.WAHA_BASE_URL,
            headers=headers,
            timeout=httpx.Timeout(30.0),
        )
        logger.info(
            "WhatsAppAdapter initialized for WAHA base_url=%s session=%s",
            self._settings.WAHA_BASE_URL,
            self._settings.WAHA_SESSION,
        )

        if self._settings.WAHA_AUTO_CONFIGURE:
            try:
                await self.ensure_session_and_webhook()
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "WAHA auto-configuration skipped (WAHA may still be booting): %s",
                    exc,
                )

        try:
            await self._sync_contact_aliases()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "WAHA contact alias sync skipped (WAHA may still be syncing): %s",
                exc,
            )

    async def shutdown(self) -> None:
        """Close the shared httpx.AsyncClient and clear any pending state."""
        if self._client is not None:
            await self._client.aclose()
            logger.info("WhatsAppAdapter HTTP client closed.")
        self._client = None
        self._pending_buffer = None

    async def ensure_session_and_webhook(self) -> dict[str, Any]:
        """Provision or update the WAHA session and webhook without needless restarts."""
        if self._client is None:
            raise RuntimeError("WhatsAppAdapter HTTP client is not initialized.")

        session = self._settings.WAHA_SESSION
        webhooks = build_waha_webhooks(
            webhook_url=self._settings.WAHA_WEBHOOK_URL,
            webhook_secret=self._settings.WAHA_WEBHOOK_SECRET,
        )

        get_resp = await self._client.get(f"/api/sessions/{session}")
        if get_resp.status_code == 404:
            logger.info("Creating WAHA session %r with webhook config.", session)
            create_resp = await self._client.post(
                "/api/sessions",
                json={
                    "name": session,
                    "start": True,
                    "config": {"webhooks": webhooks},
                },
            )
            create_resp.raise_for_status()
            return create_resp.json()

        get_resp.raise_for_status()
        session_data: dict[str, Any] = get_resp.json()
        existing_webhooks = (session_data.get("config") or {}).get("webhooks") or []

        if not _webhooks_match(existing_webhooks, webhooks):
            logger.info("Updating WAHA session %r webhook configuration.", session)
            put_resp = await self._client.put(
                f"/api/sessions/{session}",
                json={"config": {"webhooks": webhooks}},
            )
            put_resp.raise_for_status()
            if put_resp.content:
                session_data = put_resp.json()

        status_str = str(session_data.get("status") or "").upper()
        if status_str == "STOPPED":
            logger.info("Starting STOPPED WAHA session %r.", session)
            start_resp = await self._client.post(f"/api/sessions/{session}/start")
            start_resp.raise_for_status()
            if start_resp.content:
                session_data = start_resp.json()

        return session_data

    async def _post_send_text(self, text: str) -> None:
        """Send a text message through WAHA /api/sendText."""
        if self._client is None:
            raise RuntimeError("WhatsAppAdapter is not started.")

        payload = {
            "session": self._settings.WAHA_SESSION,
            "chatId": _format_send_chat_id(self._settings.INSTINCT_WA_NUMBER),
            "text": text,
        }
        try:
            response = await self._client.post("/api/sendText", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"WAHA /api/sendText returned HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Failed to communicate with WAHA at {self._settings.WAHA_BASE_URL}: {exc}"
            ) from exc

    async def stream_message(self, prompt: str) -> AsyncIterator[str]:
        """Send prompt and yield inbound WhatsApp burst chunks as they arrive."""
        if self._client is None:
            raise RuntimeError("WhatsAppAdapter is not started.")

        buffer = _PendingBuffer(
            messages=[],
            seen_ids=set(),
            notify_event=asyncio.Event(),
        )
        self._pending_buffer = buffer

        try:
            await self._post_send_text(prompt)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._settings.RESPONSE_TIMEOUT_SECONDS
            emitted_count = 0

            while True:
                buffer.notify_event.clear()

                while emitted_count < len(buffer.messages):
                    chunk = buffer.messages[emitted_count]
                    yield chunk if emitted_count == 0 else f"\n\n{chunk}"
                    emitted_count += 1

                now = loop.time()
                remaining = deadline - now
                if remaining <= 0:
                    break

                if emitted_count == 0:
                    try:
                        await asyncio.wait_for(
                            buffer.notify_event.wait(), timeout=remaining
                        )
                    except asyncio.TimeoutError:
                        break
                else:
                    anchor = (
                        buffer.last_activity_at
                        if buffer.last_activity_at is not None
                        else now
                    )
                    settle_remaining = self._settings.REPLY_SETTLE_SECONDS - (
                        now - anchor
                    )
                    if settle_remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(
                            buffer.notify_event.wait(),
                            timeout=min(settle_remaining, remaining),
                        )
                    except asyncio.TimeoutError:
                        break

            if emitted_count == 0:
                raise TimeoutError(
                    "Timed out waiting for a response from WhatsApp via WAHA webhook."
                )
        finally:
            self._pending_buffer = None

    async def send_message(self, prompt: str) -> str:
        """Send prompt and return combined webhook bursts."""
        parts: list[str] = []
        async for chunk in self.stream_message(prompt=prompt):
            parts.append(chunk)
        return "".join(parts)

    async def handle_webhook_payload(self, payload: dict[str, Any]) -> None:
        """Process an inbound WAHA webhook event and signal the active buffer."""
        event_name = payload.get("event")
        if event_name not in {"message", "message.any"}:
            return

        session = payload.get("session")
        if session and str(session) != self._settings.WAHA_SESSION:
            return

        msg = payload.get("payload")
        if not isinstance(msg, dict):
            return

        if bool(msg.get("fromMe", False)):
            return

        buffer = self._pending_buffer
        if buffer is None:
            return

        sender = str(msg.get("from") or "").strip()
        if not sender:
            return

        if not self._is_allowed_sender(sender):
            target_digits = _digits_only(self._settings.INSTINCT_WA_NUMBER)
            key_obj = (msg.get("_data") or {}).get("key") or (
                payload.get("_data") or {}
            ).get("key")
            if target_digits and isinstance(key_obj, dict):
                for alt_field in ("remoteJidAlt", "participantAlt"):
                    alt_jid = key_obj.get(alt_field)
                    if (
                        isinstance(alt_jid, str)
                        and _digits_only(alt_jid) == target_digits
                    ):
                        normalized_sender = sender.lower()
                        self._allowed_sender_ids.add(normalized_sender)
                        logger.info(
                            "Auto-trusted WhatsApp sender %s via _data.key.%s (%s)",
                            normalized_sender,
                            alt_field,
                            alt_jid,
                        )
                        break

        if not self._is_allowed_sender(sender):
            if sender.lower().endswith("@lid") and not buffer.alias_synced:
                buffer.alias_synced = True
                try:
                    await self._sync_contact_aliases()
                except (httpx.HTTPError, ValueError) as exc:
                    logger.debug("Lazy WAHA alias sync failed: %s", exc)
            if not self._is_allowed_sender(sender):
                return

        body = msg.get("body")
        if not isinstance(body, str) or not body.strip():
            return

        msg_id = str(msg.get("id") or "")
        if msg_id:
            if msg_id in buffer.seen_ids:
                return
            buffer.seen_ids.add(msg_id)

        buffer.messages.append(body.strip())
        buffer.last_activity_at = asyncio.get_running_loop().time()
        buffer.notify_event.set()
