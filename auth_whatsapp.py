"""Backwards-compatible WhatsApp (WAHA) CLI wrapper delegating to setup.py."""

import argparse
import asyncio
from pathlib import Path
import sys
from typing import Any

import httpx

from setup import (
    _extract_instinct_chat_id,
    discover_instinct_chat,
    run_setup,
)

_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Auto-configure WAHA secrets/webhooks, authenticate WhatsApp via terminal "
            "QR code, and auto-discover Instinct's chat ID into .env."
        ),
        epilog=(
            "Examples:\n"
            "  python auth_whatsapp.py\n"
            "  python auth_whatsapp.py --timeout 180 --poll-interval 2.0\n"
            "  python setup.py --platform whatsapp"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"Max seconds to wait for QR scan and WORKING status (default: {_DEFAULT_TIMEOUT_SECONDS}).",
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


async def authenticate(
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    env_path: Path = Path(".env"),
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Delegate WhatsApp setup & QR authentication to the unified setup module."""
    result = await run_setup(
        platform="WHATSAPP",
        timeout=timeout,
        poll_interval=poll_interval,
        env_path=env_path,
        interactive=False,
        http_client=client,
    )
    target = result.get("target")
    return {
        "status": "WORKING",
        "me": result.get("me") or {},
        "instinct_chat_id": None if target == "<unset>" else target,
    }


def main() -> None:
    """Entrypoint for standalone WhatsApp WAHA setup and QR authentication."""
    args = _build_parser().parse_args()
    try:
        asyncio.run(
            authenticate(
                timeout=args.timeout,
                poll_interval=args.poll_interval,
                env_path=args.env_file,
            )
        )
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
        print("\nAuthentication cancelled.", file=sys.stderr)
        sys.exit(130)


__all__ = [
    "_extract_instinct_chat_id",
    "authenticate",
    "discover_instinct_chat",
    "main",
]


if __name__ == "__main__":
    main()
