# AGENTS.md

Engineering rules and invariants for `instinct-openai-proxy`.

## 1. Project Purpose & Stack

Open-source OpenAI-compatible proxy (`POST /v1/chat/completions`, `GET /v1/models`, `POST /webhooks/whatsapp`, `GET /health`) bridging standard OpenAI SDK clients to stateful messaging bots with non-streaming JSON and SSE streaming.

- **Stack:** `FastAPI`, `Pydantic v2` (`pydantic-settings`), `Telethon` + `qrcode` (Telegram MTProto + `--qr`/`--otp` auth), `httpx` (WhatsApp WAHA REST + Webhooks + terminal QR auth), and `pytest` + `pytest-asyncio`.
- **Module Map:**
  - `main.py` — FastAPI app, `lifespan`, `verify_api_key`, `_create_adapter()`, `/v1/*` (`JSON` & `StreamingResponse`), `/webhooks/whatsapp`, and `/health`.
  - `config.py` — `Settings(BaseSettings)` + `settings` singleton loaded from `.env` (`ACTIVE_PLATFORM`, `REPLY_SETTLE_SECONDS`, `RESPONSE_TIMEOUT_SECONDS`, `TG_SESSION_DIR`, `tg_session_path()`, Telegram & WAHA keys, `WAHA_WEBHOOK_URL`, `WAHA_AUTO_CONFIGURE`).
  - `setup.py` — Unified interactive & non-interactive onboarding wizard (`--platform whatsapp|telegram`, `--qr`/`--otp`), terminal QR rendering, Instinct `@lid` discovery (default `16508702892@c.us`), and `.env` setup.
  - `auth_telegram.py` / `auth_whatsapp.py` — Thin backwards-compatible CLI wrappers delegating to `setup.py`.
  - `core/env_manager.py` — Idempotent `.env` helpers (`ensure_env_file`, `read_env_dict`, `upsert_env_var`, `ensure_proxy_and_waha_secrets` with `sk-instinct-` generation and Docker `WAHA_API_KEY` sync fallback).
  - `core/models.py` — Standard OpenAI schemas (`Message`, `ChatCompletionRequest` with `stream: bool = False`, `Choice`, `Usage`, `ChatCompletionResponse`).
  - `core/queue_manager.py` — `RequestQueueManager` (`asyncio.Lock`, `process_request()`, `stream_chat_completion()`, latest-user-prompt extraction).
  - `adapters/base_adapter.py` — `BaseAdapter` ABC (`startup`, `shutdown`, `send_message(prompt)`, `stream_message(prompt)`).
  - `adapters/telegram_adapter.py` — `TelegramAdapter` with multi-message + edit silence-window accumulation and incremental delta streaming.
  - `adapters/whatsapp_adapter.py` — `WhatsAppAdapter` with WAHA `/api/sendText`, `ensure_session_and_webhook()`, `@c.us` <-> `@lid` alias sync (`_sync_contact_aliases()`, `_data.key.remoteJidAlt` auto-trust, lazy webhook resolution), webhook burst buffer, and `stream_message()`.
  - `tests/test_proxy.py` — Async `pytest` test suite covering auth, latest-user-prompt extraction, concurrency locking, `@c.us` <-> `@lid` resolution, webhooks, and SSE streaming.

## 2. Strict Async & Concurrency Invariants

- **Non-blocking I/O only:** All network, subprocess, and wait operations MUST use `asyncio`, `Telethon`, `asyncio.create_subprocess_exec`, or `httpx.AsyncClient`. Never use synchronous `requests` or `time.sleep()`.
- **Event-loop safety (Python 3.14+):** Instantiate event-loop-bound clients (`TelegramClient`, `httpx.AsyncClient`) inside `async def startup()`, never in sync `__init__` or at module import time.
- **Single-concurrency lock (non-streaming & SSE):** Upstream bot conversations share a single stateful thread. Every completion MUST execute inside `async with self._lock:` (`RequestQueueManager.process_request()` and `RequestQueueManager.stream_chat_completion()`), holding the lock inside the SSE async generator until completion, timeout, or disconnect.
- **Multi-message silence debounce:** Both `TelegramAdapter` and `WhatsAppAdapter` accumulate multi-message bursts in order until `REPLY_SETTLE_SECONDS` of silence elapses after the first message, bounded overall by `RESPONSE_TIMEOUT_SECONDS`, and join parts with `"\n\n"`.

## 3. Adapter Contract & Stateful Memory Model

- **Unified adapter interface:** All platform adapters MUST subclass `BaseAdapter` (`adapters/base_adapter.py`), implement `startup()`, `shutdown()`, `send_message(prompt: str)`, and `stream_message(prompt: str)`, and be wired in `_create_adapter()` (`main.py`) via `ACTIVE_PLATFORM`.
- **Stateful memory-backed assistant:** Instinct (`@textinstinct`) maintains persistent account-level conversational memory across turns on both platforms, so `RequestQueueManager.aggregate_messages()` forwards only the latest `user` prompt directly without synthetic reset round-trips or redundant history replay.
- **No interactive prompts in FastAPI:** Session login belongs exclusively in `setup.py` (and `auth_telegram.py` / `auth_whatsapp.py` wrappers). `TelegramAdapter.startup()` must fail fast if unauthorized.
- **WhatsApp (WAHA) provisioning, alias sync & webhook rules:** When `WAHA_AUTO_CONFIGURE=True`, `WhatsAppAdapter.startup()` calls `ensure_session_and_webhook()` and `_sync_contact_aliases()` (`/lids/pn/{digits}` + `/chats/overview` & `/chats` with `limit=50`, plus `_data.key.remoteJidAlt`/`participantAlt` trust and lazy `@lid` sync on inbound webhooks). `handle_webhook_payload()` processes only `message`/`message.any` events with `fromMe=false`, allowed sender match, and deduplicated message IDs. `/webhooks/whatsapp` enforces `X-Webhook-Secret` when `WAHA_WEBHOOK_SECRET` is configured.

## 4. Open-Source Security & Hygiene

- **Zero hardcoded personal secrets:** Never hardcode personal phone numbers, user-scoped `@lid` IDs, bot usernames, or real API keys in source files, tests, docs, or `.env.example` (only `16508702892@c.us` as Instinct's public contact ID). Always read credentials from `settings` (`config.py` / `.env`).
- **Bearer auth on `/v1/*`:** `verify_api_key` enforces `Authorization: Bearer <PROXY_API_KEY>` using `secrets.compare_digest`. Only an empty `PROXY_API_KEY` bypasses auth for local development.
- **Git & Docker hygiene:** Keep `.gitignore` and `.dockerignore` excluding `.env`, `*.session`, `*.session-journal`, `sessions/`, `waha-sessions/`, `__pycache__/`, and `.venv/`.
