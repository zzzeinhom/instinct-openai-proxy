"""Unified interactive setup and authentication CLI for instinct-openai-proxy."""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
import getpass
from pathlib import Path
import re
import sys
from typing import Any

import httpx
import qrcode
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from adapters.whatsapp_adapter import WhatsAppAdapter
from config import Settings
from core.env_manager import (
    ensure_env_file,
    ensure_proxy_and_waha_secrets,
    read_env_dict,
    upsert_env_var,
)

InputFn = Callable[[str], Awaitable[str]]

_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_QR_MAX_REFRESHES = 5
_QR_TIMEOUT_SECONDS = 30
_PLACEHOLDER_PHONE = "+1234567890"
DEFAULT_INSTINCT_WA_NUMBER = "16508702892@c.us"
_DEFAULT_INSTINCT_DIGITS = "16508702892"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Interactive setup and authentication wizard for instinct-openai-proxy "
            "(WhatsApp via WAHA or Telegram via MTProto)."
        ),
        epilog=(
            "Examples:\n"
            "  python setup.py                               # Interactive setup wizard\n"
            "  python setup.py --platform whatsapp           # Non-interactive WhatsApp QR setup\n"
            "  python setup.py --platform telegram --qr      # Non-interactive Telegram QR setup\n"
            "  python setup.py --platform telegram --otp     # Non-interactive Telegram OTP setup"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--platform",
        type=str.lower,
        choices=["whatsapp", "telegram"],
        default=None,
        help="Target messaging platform (omit to select interactively).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--qr",
        action="store_true",
        help="Use Telegram QR login (default for Telegram).",
    )
    mode_group.add_argument(
        "--otp",
        action="store_true",
        help="Use Telegram phone number + OTP login instead of QR.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"Max seconds to wait for WAHA QR scan (default: {_DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=_DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Seconds between WAHA session status polls (default: {_DEFAULT_POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to the .env file to initialize and update (default: .env).",
    )
    return parser


async def _default_input(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def _default_password(prompt: str) -> str:
    return await asyncio.to_thread(getpass.getpass, prompt)


def print_qr_ascii(value: str) -> None:
    """Render a QR payload as a scannable ASCII QR code in the terminal."""
    qr = qrcode.QRCode(border=1)
    qr.add_data(value)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def normalize_wa_number(raw: str) -> str:
    """Normalize a phone number or chat ID into a WAHA-compatible WhatsApp JID."""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    if "@" in cleaned:
        return cleaned
    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return ""
    return f"{digits}@c.us"


def _extract_id_field(raw_id: Any) -> str:
    if isinstance(raw_id, str):
        return raw_id.strip()
    if isinstance(raw_id, dict):
        serialized = raw_id.get("_serialized")
        if isinstance(serialized, str):
            return serialized.strip()
    return ""


def _extract_instinct_chat_id(
    chats: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """Search WAHA chats (top-level and nested _chat) for Instinct and prefer @lid."""
    for chat in chats:
        if not isinstance(chat, dict):
            continue

        nested_chat = chat.get("_chat") if isinstance(chat.get("_chat"), dict) else {}
        sources = [chat, nested_chat] if nested_chat else [chat]

        candidates: list[str] = []
        jids: list[str] = []
        for src in sources:
            for field in (
                "displayName",
                "username",
                "name",
                "pushname",
                "shortName",
                "formattedName",
            ):
                val = src.get(field)
                if isinstance(val, str) and val.strip():
                    candidates.append(val.strip())
            for jid_field in ("pnJid", "id", "accountLid", "lid"):
                jid_val = _extract_id_field(src.get(jid_field))
                if jid_val:
                    jids.append(jid_val)

        contact = chat.get("contact")
        if isinstance(contact, dict):
            for field in (
                "displayName",
                "username",
                "name",
                "pushname",
                "shortName",
                "formattedName",
            ):
                val = contact.get(field)
                if isinstance(val, str) and val.strip():
                    candidates.append(val.strip())

        matched_label = next(
            (label for label in candidates if "instinct" in label.lower()),
            None,
        )
        if matched_label is None and any(
            _DEFAULT_INSTINCT_DIGITS in jid for jid in jids
        ):
            matched_label = DEFAULT_INSTINCT_WA_NUMBER

        if matched_label is None:
            continue

        lid = (
            _extract_id_field(chat.get("accountLid"))
            or _extract_id_field(nested_chat.get("accountLid"))
            or _extract_id_field(chat.get("lid"))
            or _extract_id_field(nested_chat.get("lid"))
        )
        chat_id = (
            lid
            or _extract_id_field(chat.get("id"))
            or _extract_id_field(nested_chat.get("id"))
            or _extract_id_field(chat.get("pnJid"))
            or _extract_id_field(nested_chat.get("pnJid"))
        )
        if chat_id:
            return chat_id, matched_label

    return None


def _coerce_chat_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("chats", "data", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict)]
    return []


async def resolve_phone_to_lid(
    client: httpx.AsyncClient, session: str, phone_or_jid: str
) -> str | None:
    """Resolve a phone number or @c.us JID to its @lid via WAHA /lids/pn/{digits}."""
    local_part = phone_or_jid.strip().split("@", 1)[0]
    digits = re.sub(r"\D", "", local_part)
    if not digits:
        return None
    try:
        resp = await client.get(f"/api/{session}/lids/pn/{digits}")
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None

    lid = data.get("lid") if isinstance(data, dict) else None
    if isinstance(lid, str) and lid.strip():
        return lid.strip()
    return None


async def discover_instinct_chat(
    client: httpx.AsyncClient,
    session: str,
    phone_hint: str = DEFAULT_INSTINCT_WA_NUMBER,
) -> tuple[str, str] | None:
    """Query WAHA chats endpoints (with limit=50) and fallback to /lids/pn/."""
    for endpoint in (f"/api/{session}/chats", f"/api/{session}/chats/overview"):
        try:
            resp = await client.get(endpoint, params={"limit": 50})
            if resp.status_code != 200:
                continue
            chats = _coerce_chat_list(resp.json())
        except (httpx.HTTPError, ValueError):
            continue

        found = _extract_instinct_chat_id(chats)
        if found is not None:
            return found

    if phone_hint:
        lid = await resolve_phone_to_lid(client, session, phone_hint)
        if lid is not None:
            return lid, phone_hint

    return None


def _is_valid_tg_api_id(raw: str) -> bool:
    try:
        return int(raw.strip()) > 0
    except (TypeError, ValueError):
        return False


def _is_placeholder_tg_hash(raw: str) -> bool:
    cleaned = raw.strip()
    return not cleaned or cleaned.lower().startswith("your_")


def _is_placeholder_phone(raw: str) -> bool:
    cleaned = raw.strip()
    return not cleaned or cleaned == _PLACEHOLDER_PHONE


async def setup_whatsapp(
    env_path: Path = Path(".env"),
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    interactive: bool = True,
    input_fn: InputFn = _default_input,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Run WhatsApp (WAHA) provisioning, terminal QR scan, and chat discovery."""
    env_values = read_env_dict(env_path=env_path)
    current_url = env_values.get("WAHA_BASE_URL") or "http://localhost:3000"

    if interactive:
        entered_url = (
            await input_fn(f"WAHA Base URL [{current_url}]: ")
        ).strip()
        if entered_url:
            upsert_env_var(
                "WAHA_BASE_URL", entered_url.rstrip("/"), env_path=env_path
            )
        elif not env_values.get("WAHA_BASE_URL"):
            upsert_env_var("WAHA_BASE_URL", current_url, env_path=env_path)

    runtime_settings = Settings(_env_file=env_path)
    headers: dict[str, str] = {}
    if runtime_settings.WAHA_API_KEY:
        headers["X-Api-Key"] = runtime_settings.WAHA_API_KEY

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        base_url=runtime_settings.WAHA_BASE_URL,
        headers=headers,
        timeout=httpx.Timeout(30.0),
    )

    try:
        adapter = WhatsAppAdapter(settings=runtime_settings)
        adapter._client = client
        await adapter.ensure_session_and_webhook()

        session = runtime_settings.WAHA_SESSION
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1, timeout)
        last_qr_value: str | None = None
        me: dict[str, Any] = {}

        while True:
            session_resp = await client.get(f"/api/sessions/{session}")
            session_resp.raise_for_status()
            session_data = session_resp.json()
            status_str = str(session_data.get("status") or "").upper()

            if status_str == "WORKING":
                me = session_data.get("me") or {}
                wa_id = me.get("id") or "<unknown>"
                push_name = me.get("pushName") or "<unknown>"
                print(f"WhatsApp session {session!r} is WORKING.")
                print(f"Authenticated as {push_name} (ID: {wa_id}).")
                break

            if status_str == "SCAN_QR_CODE":
                qr_resp = await client.get(
                    f"/api/{session}/auth/qr",
                    params={"format": "raw"},
                    headers={"Accept": "application/json"},
                )
                if qr_resp.status_code == 200:
                    qr_data = qr_resp.json()
                    qr_value = qr_data.get("value")
                    if (
                        isinstance(qr_value, str)
                        and qr_value
                        and qr_value != last_qr_value
                    ):
                        last_qr_value = qr_value
                        print("\nScan this QR code in WhatsApp:")
                        print("  WhatsApp -> Linked Devices -> Link a Device\n")
                        print_qr_ascii(qr_value)
            elif status_str == "STOPPED":
                start_resp = await client.post(f"/api/sessions/{session}/start")
                start_resp.raise_for_status()

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out after {timeout}s waiting for WAHA session {session!r} to reach WORKING."
                )
            await asyncio.sleep(min(poll_interval, remaining))

        existing_target = read_env_dict(env_path=env_path).get(
            "INSTINCT_WA_NUMBER", ""
        )
        has_custom = bool(existing_target) and "X" not in existing_target
        default_target = (
            existing_target if has_custom else DEFAULT_INSTINCT_WA_NUMBER
        )

        discovered = await discover_instinct_chat(
            client, session, phone_hint=default_target
        )
        target_chat_id: str | None = None
        if discovered is not None:
            target_chat_id, matched_label = discovered
            upsert_env_var(
                "INSTINCT_WA_NUMBER", target_chat_id, env_path=env_path
            )
            print(
                f"Discovered Instinct chat ({matched_label}) -> "
                f"INSTINCT_WA_NUMBER={target_chat_id}"
            )
        else:
            print(
                "\nCould not auto-discover an existing 'Instinct' chat in WhatsApp history yet."
            )
            if interactive:
                manual_raw = (
                    await input_fn(
                        f"Enter Instinct's WhatsApp number or chatId [{default_target}]: "
                    )
                ).strip()
                if manual_raw:
                    normalized = normalize_wa_number(manual_raw)
                    resolved_lid = (
                        await resolve_phone_to_lid(client, session, normalized)
                        if not normalized.lower().endswith("@lid")
                        else None
                    )
                    target_chat_id = resolved_lid or normalized
                else:
                    target_chat_id = default_target
            else:
                target_chat_id = default_target

            upsert_env_var(
                "INSTINCT_WA_NUMBER", target_chat_id, env_path=env_path
            )
            print(f"Configured INSTINCT_WA_NUMBER={target_chat_id}")

        return {
            "platform": "WHATSAPP",
            "session_status": f"WAHA session '{session}' (WORKING)",
            "me": me,
            "target": target_chat_id or "<unset>",
        }
    finally:
        if owns_client:
            await client.aclose()


async def _telegram_qr_login(
    client: TelegramClient,
    password_fn: InputFn,
) -> None:
    """Authenticate Telegram via terminal QR code with auto-refresh and 2FA."""
    qr_login = await client.qr_login()
    for attempt in range(1, _QR_MAX_REFRESHES + 1):
        print(
            f"\nScan this QR code in Telegram (attempt {attempt}/{_QR_MAX_REFRESHES}):"
        )
        print("  Telegram -> Settings -> Devices -> Link Desktop Device\n")
        print_qr_ascii(qr_login.url)

        try:
            await qr_login.wait(timeout=_QR_TIMEOUT_SECONDS)
            return
        except asyncio.TimeoutError:
            if attempt == _QR_MAX_REFRESHES:
                raise TimeoutError(
                    "QR code login timed out after maximum refreshes. Re-run 'python setup.py --platform telegram --qr'."
                )
            print("QR code expired; generating a fresh QR code...")
            await qr_login.recreate()
        except SessionPasswordNeededError:
            password = await password_fn("Enter 2FA Cloud Password: ")
            await client.sign_in(password=password)
            return


async def _telegram_otp_login(
    client: TelegramClient,
    phone_number: str,
    input_fn: InputFn,
    password_fn: InputFn,
) -> None:
    """Authenticate Telegram via OTP code and optional 2FA cloud password."""
    print(f"Sending login code to {phone_number}...")
    print(
        "Tip: If Telegram does not deliver the OTP, re-run with: python setup.py --platform telegram --qr"
    )
    await client.send_code_request(phone_number)

    code = (await input_fn("Enter the Telegram login code (OTP): ")).strip()
    if not code:
        raise ValueError("OTP code cannot be empty.")

    try:
        await client.sign_in(phone=phone_number, code=code)
    except SessionPasswordNeededError:
        password = await password_fn("Enter 2FA Cloud Password: ")
        await client.sign_in(password=password)


async def setup_telegram(
    env_path: Path = Path(".env"),
    auth_method: str | None = None,
    interactive: bool = True,
    input_fn: InputFn = _default_input,
    password_fn: InputFn = _default_password,
    tg_client: Any = None,
) -> dict[str, Any]:
    """Run Telegram credential prompting, QR/OTP login, and .session persistence."""
    env_values = read_env_dict(env_path=env_path)
    api_id_raw = env_values.get("TG_API_ID", "")
    api_hash_raw = env_values.get("TG_API_HASH", "")
    can_prompt = interactive or tg_client is not None or sys.stdin.isatty()

    if not _is_valid_tg_api_id(api_id_raw) or _is_placeholder_tg_hash(api_hash_raw):
        if not can_prompt:
            raise ValueError(
                "Missing TG_API_ID or TG_API_HASH in .env. Run 'python setup.py' interactively or set them in .env."
            )
        print(
            "\nGet your API ID & Hash from https://my.telegram.org -> API development tools"
        )
        while not _is_valid_tg_api_id(api_id_raw):
            entered_id = (await input_fn("Enter Telegram TG_API_ID: ")).strip()
            if _is_valid_tg_api_id(entered_id):
                api_id_raw = str(int(entered_id))
                upsert_env_var("TG_API_ID", api_id_raw, env_path=env_path)
            else:
                print("TG_API_ID must be a positive integer.")

        while _is_placeholder_tg_hash(api_hash_raw):
            entered_hash = (await input_fn("Enter Telegram TG_API_HASH: ")).strip()
            if not _is_placeholder_tg_hash(entered_hash):
                api_hash_raw = entered_hash
                upsert_env_var("TG_API_HASH", api_hash_raw, env_path=env_path)
            else:
                print("TG_API_HASH cannot be empty.")

    current_username = (
        env_values.get("INSTINCT_TG_USERNAME")
        or Settings(_env_file=env_path).INSTINCT_TG_USERNAME
    )
    if interactive:
        entered_username = (
            await input_fn(
                f"Instinct Telegram username (e.g. @textinstinct) [{current_username}]: "
            )
        ).strip()
        if entered_username:
            current_username = entered_username
            upsert_env_var(
                "INSTINCT_TG_USERNAME", current_username, env_path=env_path
            )

    chosen_method = auth_method
    if chosen_method is None:
        if interactive:
            print("\nSelect Telegram login method:")
            print("  [1] QR Code scan (Recommended — no phone number needed)")
            print("  [2] Phone number + OTP code")
            choice = (await input_fn("Enter choice [1/2] (default 1): ")).strip()
            chosen_method = "otp" if choice == "2" else "qr"
        else:
            chosen_method = "qr"

    phone_number = env_values.get("TG_PHONE_NUMBER", "")
    if chosen_method == "otp" and _is_placeholder_phone(phone_number):
        if not can_prompt:
            raise ValueError(
                "TG_PHONE_NUMBER is required for --otp login. Set it in .env or use --qr."
            )
        while _is_placeholder_phone(phone_number):
            entered_phone = (
                await input_fn(
                    "Enter Telegram phone number (E.164 format, e.g. +2010...): "
                )
            ).strip()
            if not _is_placeholder_phone(entered_phone):
                phone_number = entered_phone
                upsert_env_var("TG_PHONE_NUMBER", phone_number, env_path=env_path)
            else:
                print("Please enter a valid E.164 phone number.")

    runtime_settings = Settings(_env_file=env_path)
    runtime_settings.validate_telegram_config(
        require_phone=(chosen_method == "otp")
    )
    session_target = runtime_settings.tg_session_path()
    session_path = Path(f"{session_target}.session").resolve()

    owns_client = tg_client is None
    client = tg_client or TelegramClient(
        session_target,
        runtime_settings.TG_API_ID,
        runtime_settings.TG_API_HASH,
    )

    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            username = (
                f"@{me.username}"
                if getattr(me, "username", None)
                else getattr(me, "first_name", "user")
            )
            print(f"Already authenticated as {username} (ID: {me.id}).")
            print(f"Session file: {session_path}")
        else:
            if chosen_method == "otp":
                await _telegram_otp_login(
                    client, phone_number, input_fn=input_fn, password_fn=password_fn
                )
            else:
                await _telegram_qr_login(client, password_fn=password_fn)

            me = await client.get_me()
            username = (
                f"@{me.username}"
                if getattr(me, "username", None)
                else getattr(me, "first_name", "user")
            )
            print(f"Successfully authenticated as {username} (ID: {me.id}).")
            print(f"Saved session file to: {session_path}")

        return {
            "platform": "TELEGRAM",
            "session_status": f"Authorized ({session_path})",
            "me": me,
            "target": runtime_settings.INSTINCT_TG_USERNAME,
        }
    finally:
        if owns_client and client.is_connected():
            await client.disconnect()


def _print_ready_summary(
    platform: str,
    session_status: str,
    target: str,
    proxy_api_key: str,
) -> None:
    print("\n=== instinct-openai-proxy Setup Complete ===")
    print(f"  ACTIVE_PLATFORM : {platform}")
    print(f"  Session         : {session_status}")
    print(f"  Instinct Target : {target}")
    print(f"  PROXY_API_KEY   : {proxy_api_key}")
    print("\nStart the proxy server with:")
    print("  uvicorn main:app --host 0.0.0.0 --port 8000 --reload")


async def run_setup(
    platform: str | None = None,
    auth_method: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    env_path: Path = Path(".env"),
    interactive: bool | None = None,
    input_fn: InputFn = _default_input,
    password_fn: InputFn = _default_password,
    http_client: httpx.AsyncClient | None = None,
    tg_client: Any = None,
) -> dict[str, Any]:
    """Run the unified setup and authentication workflow for WhatsApp or Telegram."""
    ensure_env_file(env_path=env_path)
    secrets_map = await ensure_proxy_and_waha_secrets(
        env_path=env_path, client=http_client
    )

    is_interactive = (platform is None) if interactive is None else interactive

    selected_platform = platform.strip().upper() if platform else ""
    if not selected_platform:
        print("Select messaging platform for Instinct Proxy:")
        print("  [1] WhatsApp (via WAHA — terminal QR scan, no API ID required)")
        print("  [2] Telegram (via MTProto — terminal QR scan or OTP)")
        while True:
            choice = (await input_fn("Enter choice [1/2]: ")).strip()
            if choice in {"1", "whatsapp"}:
                selected_platform = "WHATSAPP"
                break
            if choice in {"2", "telegram"}:
                selected_platform = "TELEGRAM"
                break
            print("Invalid choice. Please enter 1 (WhatsApp) or 2 (Telegram).")

    if selected_platform not in {"WHATSAPP", "TELEGRAM"}:
        raise ValueError(
            f"Unsupported platform {selected_platform!r}. Choose 'whatsapp' or 'telegram'."
        )

    upsert_env_var("ACTIVE_PLATFORM", selected_platform, env_path=env_path)

    if selected_platform == "WHATSAPP":
        result = await setup_whatsapp(
            env_path=env_path,
            timeout=timeout,
            poll_interval=poll_interval,
            interactive=is_interactive,
            input_fn=input_fn,
            http_client=http_client,
        )
    else:
        result = await setup_telegram(
            env_path=env_path,
            auth_method=auth_method,
            interactive=is_interactive,
            input_fn=input_fn,
            password_fn=password_fn,
            tg_client=tg_client,
        )

    _print_ready_summary(
        platform=selected_platform,
        session_status=result["session_status"],
        target=result["target"],
        proxy_api_key=secrets_map["PROXY_API_KEY"],
    )
    result["proxy_api_key"] = secrets_map["PROXY_API_KEY"]
    result["env_path"] = str(env_path.resolve())
    return result


def main() -> None:
    """CLI entrypoint for unified instinct-openai-proxy setup and authentication."""
    args = _build_parser().parse_args()
    auth_method: str | None = None
    if args.otp:
        auth_method = "otp"
    elif args.qr:
        auth_method = "qr"

    try:
        asyncio.run(
            run_setup(
                platform=args.platform,
                auth_method=auth_method,
                timeout=args.timeout,
                poll_interval=args.poll_interval,
                env_path=args.env_file,
            )
        )
    except (PhoneCodeInvalidError,):
        print("Error: The OTP code entered is invalid.", file=sys.stderr)
        sys.exit(1)
    except PhoneCodeExpiredError:
        print("Error: The OTP code has expired. Re-run setup.", file=sys.stderr)
        sys.exit(1)
    except FloodWaitError as exc:
        print(
            f"Error: Telegram rate limit reached. Wait {exc.seconds} seconds before retrying.",
            file=sys.stderr,
        )
        sys.exit(1)
    except httpx.HTTPStatusError as exc:
        print(
            f"WAHA HTTP error {exc.response.status_code}: {exc.response.text}",
            file=sys.stderr,
        )
        sys.exit(1)
    except httpx.HTTPError as exc:
        print(
            f"Failed to connect to WAHA ({exc}). Ensure WAHA is running.",
            file=sys.stderr,
        )
        sys.exit(1)
    except (ValueError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
