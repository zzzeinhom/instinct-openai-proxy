"""Automated pytest suite for instinct-openai-proxy."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from adapters.base_adapter import BaseAdapter
from adapters.telegram_adapter import TelegramAdapter
from adapters.whatsapp_adapter import WhatsAppAdapter
from config import Settings
from core.models import ChatCompletionRequest, Message
from core.queue_manager import RequestQueueManager
import main


class _StubAdapter(BaseAdapter):
    def __init__(
        self,
        chunks: list[str] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.calls: list[str] = []
        self.chunks = chunks or ["hello", " world"]
        self.delay = delay
        self.active = 0
        self.max_active = 0

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def send_message(self, prompt: str) -> str:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(prompt)
            if self.delay:
                await asyncio.sleep(self.delay)
            return "".join(self.chunks)
        finally:
            self.active -= 1

    async def stream_message(self, prompt: str):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(prompt)
            for chunk in self.chunks:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield chunk
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_bearer_auth_and_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify /v1/* rejects invalid/missing Bearer tokens and allows valid keys."""
    monkeypatch.setattr(main.settings, "PROXY_API_KEY", "test-proxy-secret")
    adapter = _StubAdapter(chunks=["pong"])
    main.app.state.adapter = adapter
    main.app.state.queue_manager = RequestQueueManager(adapter=adapter)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200

        payload = {"model": "instinct", "messages": [{"role": "user", "content": "hi"}]}
        missing = await client.post("/v1/chat/completions", json=payload)
        assert missing.status_code == 401

        wrong = await client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert wrong.status_code == 401

        ok = await client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer test-proxy-secret"},
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "pong"


@pytest.mark.asyncio
async def test_forwards_latest_user_prompt_only() -> None:
    """Verify only the latest user prompt is forwarded without history replay."""
    adapter = _StubAdapter(chunks=["ok"])
    qm = RequestQueueManager(adapter=adapter)

    await qm.process_request(
        ChatCompletionRequest(
            messages=[
                Message(role="system", content="You are helpful."),
                Message(role="user", content="First question"),
                Message(role="assistant", content="First answer"),
                Message(role="user", content="Follow-up question"),
            ]
        )
    )
    assert adapter.calls == ["Follow-up question"]

    with pytest.raises(ValueError):
        qm.aggregate_messages([Message(role="user", content="   ")])


@pytest.mark.asyncio
async def test_sequential_queue_concurrency_non_streaming_and_sse() -> None:
    """Verify concurrent non-streaming and SSE requests never overlap execution."""
    adapter = _StubAdapter(chunks=["chunk-a", "chunk-b"], delay=0.03)
    qm = RequestQueueManager(adapter=adapter)

    async def run_stream(text: str) -> list[str]:
        req = ChatCompletionRequest(
            messages=[Message(role="user", content=text)], stream=True
        )
        return [frame async for frame in qm.stream_chat_completion(req)]

    non_stream_task = asyncio.create_task(
        qm.process_request(
            ChatCompletionRequest(messages=[Message(role="user", content="sync")])
        )
    )
    stream_task_1 = asyncio.create_task(run_stream("sse-1"))
    stream_task_2 = asyncio.create_task(run_stream("sse-2"))

    await asyncio.gather(non_stream_task, stream_task_1, stream_task_2)
    assert adapter.max_active == 1


@pytest.mark.asyncio
async def test_whatsapp_alias_resolution_burst_and_webhook_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify lazy @c.us <-> @lid resolution, burst join, and X-Webhook-Secret."""
    wa_settings = Settings(
        _env_file=None,
        ACTIVE_PLATFORM="WHATSAPP",
        WAHA_BASE_URL="http://waha.test",
        WAHA_API_KEY="waha-test-key",
        WAHA_SESSION="default",
        WAHA_WEBHOOK_SECRET="hook-secret-123",
        WAHA_AUTO_CONFIGURE=False,
        INSTINCT_WA_NUMBER="16508702892@c.us",
        REPLY_SETTLE_SECONDS=0.12,
        RESPONSE_TIMEOUT_SECONDS=2.0,
    )

    sent_texts: list[dict] = []
    alias_calls = {"count": 0}

    def waha_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/default/lids/pn/16508702892":
            return httpx.Response(404, json={"error": "not found"})
        if req.url.path == "/api/default/chats/overview":
            assert req.url.params.get("limit") == "50"
            alias_calls["count"] += 1
            if alias_calls["count"] == 1:
                # Simulate initial startup before chat metadata synced
                return httpx.Response(200, json=[])
            # Lazy lookup during first @lid webhook returns nested _chat alias
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "synthetic-lid-001@lid",
                        "_chat": {
                            "id": "synthetic-lid-001@lid",
                            "pnJid": "16508702892@s.whatsapp.net",
                            "accountLid": "synthetic-lid-001@lid",
                        },
                    }
                ],
            )
        if req.url.path == "/api/default/chats":
            assert req.url.params.get("limit") == "50"
            return httpx.Response(200, json=[])
        if req.url.path == "/api/sendText":
            sent_texts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "sent-1"})
        raise AssertionError(f"Unexpected WAHA request: {req.method} {req.url}")

    adapter = WhatsAppAdapter(settings=wa_settings)
    adapter._client = httpx.AsyncClient(
        base_url="http://waha.test",
        transport=httpx.MockTransport(waha_handler),
    )
    await adapter._sync_contact_aliases()

    monkeypatch.setattr(main.settings, "WAHA_WEBHOOK_SECRET", "hook-secret-123")
    monkeypatch.setattr(main.settings, "PROXY_API_KEY", "")
    main.app.state.adapter = adapter
    main.app.state.queue_manager = RequestQueueManager(adapter=adapter)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        bad_hook = await client.post(
            "/webhooks/whatsapp",
            json={},
            headers={"X-Webhook-Secret": "wrong"},
        )
        assert bad_hook.status_code == 401

        async def fire_webhooks() -> None:
            await asyncio.sleep(0.02)
            # Ignored: fromMe=True
            await client.post(
                "/webhooks/whatsapp",
                json={
                    "event": "message",
                    "session": "default",
                    "payload": {
                        "id": "m0",
                        "fromMe": True,
                        "from": "synthetic-lid-001@lid",
                        "body": "outgoing",
                    },
                },
                headers={"X-Webhook-Secret": "hook-secret-123"},
            )
            # First @lid reply triggers lazy _sync_contact_aliases()
            await client.post(
                "/webhooks/whatsapp",
                json={
                    "event": "message",
                    "session": "default",
                    "payload": {
                        "id": "m1",
                        "fromMe": False,
                        "from": "synthetic-lid-001@lid",
                        "body": "Hello from Instinct",
                    },
                },
                headers={"X-Webhook-Secret": "hook-secret-123"},
            )
            # Ignored: unrelated sender
            await client.post(
                "/webhooks/whatsapp",
                json={
                    "event": "message",
                    "session": "default",
                    "payload": {
                        "id": "m-other",
                        "fromMe": False,
                        "from": "other-sender@lid",
                        "body": "noise",
                    },
                },
                headers={"X-Webhook-Secret": "hook-secret-123"},
            )
            await asyncio.sleep(0.03)
            # Second burst chunk from resolved @lid
            await client.post(
                "/webhooks/whatsapp",
                json={
                    "event": "message",
                    "session": "default",
                    "payload": {
                        "id": "m2",
                        "fromMe": False,
                        "from": "synthetic-lid-001@lid",
                        "body": "Follow-up part",
                    },
                },
                headers={"X-Webhook-Secret": "hook-secret-123"},
            )

        webhook_task = asyncio.create_task(fire_webhooks())
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "instinct",
                "messages": [{"role": "user", "content": "Hello?"}],
            },
        )
        await webhook_task

    await adapter.shutdown()
    assert resp.status_code == 200
    assert (
        resp.json()["choices"][0]["message"]["content"]
        == "Hello from Instinct\n\nFollow-up part"
    )
    assert sent_texts == [
        {
            "session": "default",
            "chatId": "16508702892@c.us",
            "text": "Hello?",
        }
    ]
    assert alias_calls["count"] == 2


@pytest.mark.asyncio
async def test_sse_streaming_endpoint_chunks_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify POST /v1/chat/completions with stream=True emits OpenAI SSE chunks."""
    monkeypatch.setattr(main.settings, "PROXY_API_KEY", "stream-key")
    adapter = _StubAdapter(chunks=["Hello", " world", "!"])
    main.app.state.adapter = adapter
    main.app.state.queue_manager = RequestQueueManager(adapter=adapter)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "instinct",
                "stream": True,
                "messages": [{"role": "user", "content": "Stream test"}],
            },
            headers={"Authorization": "Bearer stream-key"},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            lines = [line async for line in resp.aiter_lines() if line.startswith("data: ")]

    assert lines[-1] == "data: [DONE]"
    frames = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    assert all(frame["object"] == "chat.completion.chunk" for frame in frames)
    assert frames[0]["choices"][0]["delta"] == {
        "role": "assistant",
        "content": "Hello",
    }
    assert "".join(
        frame["choices"][0]["delta"].get("content", "") for frame in frames
    ) == "Hello world!"
    assert frames[-1]["choices"][0]["delta"] == {}
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_telegram_stream_message_incremental_deltas() -> None:
    """Verify TelegramAdapter.stream_message emits incremental deltas on edits."""
    tg_settings = Settings(
        _env_file=None,
        TG_API_ID=12345,
        TG_API_HASH="test-hash",
        INSTINCT_TG_USERNAME="@TestBot",
        REPLY_SETTLE_SECONDS=0.12,
        RESPONSE_TIMEOUT_SECONDS=2.0,
    )
    tg = TelegramAdapter(settings=tg_settings)
    handlers: list[tuple] = []

    class FakeClient:
        def is_connected(self) -> bool:
            return True

        async def is_user_authorized(self) -> bool:
            return True

        def add_event_handler(self, fn, builder) -> None:
            handlers.append((fn, builder))

        def remove_event_handler(self, fn, builder) -> None:
            handlers.remove((fn, builder))

        async def send_message(self, entity, text: str):
            sent = MagicMock()
            sent.id = 500

            async def emit() -> None:
                await asyncio.sleep(0.02)
                for fn, builder in list(handlers):
                    if builder.__class__.__name__ == "NewMessage":
                        await fn(
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    id=501, out=False, raw_text="Hel", message="Hel"
                                )
                            )
                        )
                await asyncio.sleep(0.03)
                for fn, builder in list(handlers):
                    if builder.__class__.__name__ == "MessageEdited":
                        await fn(
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    id=501,
                                    out=False,
                                    raw_text="Hello world",
                                    message="Hello world",
                                )
                            )
                        )

            asyncio.create_task(emit())
            return sent

    tg._client = FakeClient()
    tg._bot_entity = object()

    deltas = [delta async for delta in tg.stream_message("hi")]
    assert deltas == ["Hel", "lo world"]
    assert handlers == []


@pytest.mark.asyncio
async def test_waha_lids_pn_and_remote_jid_alt_and_setup_discovery() -> None:
    """Verify /lids/pn/ resolution, limit=50 params, and _data.key.remoteJidAlt trust."""
    import setup as setup_cli

    wa_settings = Settings(
        _env_file=None,
        ACTIVE_PLATFORM="WHATSAPP",
        WAHA_BASE_URL="http://waha.test",
        WAHA_SESSION="default",
        WAHA_AUTO_CONFIGURE=False,
        INSTINCT_WA_NUMBER="16508702892@c.us",
        REPLY_SETTLE_SECONDS=0.1,
        RESPONSE_TIMEOUT_SECONDS=1.0,
    )

    # 1. Adapter resolves @lid via /api/default/lids/pn/16508702892
    def lid_pn_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/default/lids/pn/16508702892":
            return httpx.Response(200, json={"lid": "direct-lid-777@lid"})
        raise AssertionError(f"Unexpected request: {req.method} {req.url}")

    adapter = WhatsAppAdapter(settings=wa_settings)
    adapter._client = httpx.AsyncClient(
        base_url="http://waha.test",
        transport=httpx.MockTransport(lid_pn_handler),
    )
    await adapter._sync_contact_aliases()
    assert "direct-lid-777@lid" in adapter._allowed_sender_ids
    await adapter.shutdown()

    # 2. handle_webhook_payload auto-trusts unknown @lid when _data.key.remoteJidAlt matches
    def empty_waha(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/sendText":
            return httpx.Response(201, json={"id": "ok"})
        return httpx.Response(404, json={})

    adapter2 = WhatsAppAdapter(settings=wa_settings)
    adapter2._client = httpx.AsyncClient(
        base_url="http://waha.test",
        transport=httpx.MockTransport(empty_waha),
    )

    async def send_alt_webhook() -> None:
        await asyncio.sleep(0.02)
        await adapter2.handle_webhook_payload(
            {
                "event": "message",
                "session": "default",
                "payload": {
                    "id": "alt-1",
                    "fromMe": False,
                    "from": "alt-lid-888@lid",
                    "body": "Trusted via remoteJidAlt",
                    "_data": {
                        "key": {
                            "remoteJidAlt": "16508702892@s.whatsapp.net"
                        }
                    },
                },
            }
        )

    task = asyncio.create_task(send_alt_webhook())
    reply = await adapter2.send_message("hello")
    await task
    assert reply == "Trusted via remoteJidAlt"
    assert "alt-lid-888@lid" in adapter2._allowed_sender_ids
    await adapter2.shutdown()

    # 3. setup.discover_instinct_chat passes limit=50 and falls back to /lids/pn/
    seen_calls: list[tuple[str, str | None]] = []

    def setup_handler(req: httpx.Request) -> httpx.Response:
        seen_calls.append((req.url.path, req.url.params.get("limit")))
        if req.url.path in {"/api/default/chats", "/api/default/chats/overview"}:
            assert req.url.params.get("limit") == "50"
            return httpx.Response(200, json=[])
        if req.url.path == "/api/default/lids/pn/16508702892":
            return httpx.Response(200, json={"lid": "setup-lid-555@lid"})
        raise AssertionError(f"Unexpected setup request: {req.method} {req.url}")

    async with httpx.AsyncClient(
        base_url="http://waha.test",
        transport=httpx.MockTransport(setup_handler),
    ) as setup_client:
        found = await setup_cli.discover_instinct_chat(setup_client, "default")

    assert found == ("setup-lid-555@lid", "16508702892@c.us")
    assert seen_calls == [
        ("/api/default/chats", "50"),
        ("/api/default/chats/overview", "50"),
        ("/api/default/lids/pn/16508702892", None),
    ]

