"""Backwards-compatible Telegram CLI wrapper delegating to setup.py."""

import argparse
import asyncio
from pathlib import Path
import sys
from typing import Any

from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
)

from setup import run_setup


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Authenticate Telegram for instinct-openai-proxy and save a Telethon .session file."
        ),
        epilog=(
            "Examples:\n"
            "  python auth_telegram.py --qr    # Recommended: scan QR code (bypasses OTP drops)\n"
            "  python auth_telegram.py --otp   # Standard SMS / in-app OTP code login\n"
            "  python setup.py --platform telegram --qr"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--qr",
        action="store_true",
        help="Authenticate via terminal ASCII QR code (recommended if OTP codes are dropped).",
    )
    mode_group.add_argument(
        "--otp",
        action="store_true",
        help="Authenticate via phone number and OTP login code (default).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to the .env file to initialize and update (default: .env).",
    )
    return parser


async def authenticate(
    use_qr: bool = False,
    env_path: Path = Path(".env"),
    tg_client: Any = None,
) -> dict[str, Any]:
    """Delegate Telegram authentication to the unified setup module."""
    return await run_setup(
        platform="TELEGRAM",
        auth_method="qr" if use_qr else "otp",
        env_path=env_path,
        interactive=False,
        tg_client=tg_client,
    )


def main() -> None:
    """Entrypoint for standalone Telegram session authentication."""
    args = _build_parser().parse_args()
    try:
        asyncio.run(authenticate(use_qr=bool(args.qr), env_path=args.env_file))
    except PhoneCodeInvalidError:
        print("Error: The OTP code entered is invalid.", file=sys.stderr)
        sys.exit(1)
    except PhoneCodeExpiredError:
        print(
            "Error: The OTP code has expired. Re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)
    except FloodWaitError as exc:
        print(
            f"Error: Telegram rate limit reached. Wait {exc.seconds} seconds before retrying.",
            file=sys.stderr,
        )
        sys.exit(1)
    except (ValueError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAuthentication cancelled.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
