"""Idempotent .env management and proxy/WAHA secret auto-provisioning helpers."""

import asyncio
import json
import logging
from pathlib import Path
import re
import secrets
import shutil

from dotenv import dotenv_values
import httpx

logger = logging.getLogger(__name__)

_DOCKER_CMD_TIMEOUT_SECONDS = 5.0
_WAHA_PROBE_TIMEOUT_SECONDS = 3.0
_PLACEHOLDER_PROXY_KEYS = {
    "",
    "local-test-key",
    "changeme",
    "your_secure_api_key",
}


def ensure_env_file(
    env_path: Path = Path(".env"),
    example_path: Path = Path(".env.example"),
) -> Path:
    """Create .env from .env.example if missing, or create an empty .env."""
    if env_path.exists():
        return env_path

    env_path.parent.mkdir(parents=True, exist_ok=True)
    if example_path.exists():
        shutil.copyfile(example_path, env_path)
    else:
        env_path.write_text("", encoding="utf-8")
    return env_path


def read_env_dict(env_path: Path = Path(".env")) -> dict[str, str]:
    """Safely parse KEY=VALUE pairs from a .env file into a stripped dictionary."""
    if not env_path.exists():
        return {}
    raw = dotenv_values(env_path)
    return {
        str(k): ("" if v is None else str(v).strip())
        for k, v in raw.items()
        if k is not None
    }


def upsert_env_var(
    key: str,
    value: str,
    env_path: Path = Path(".env"),
) -> None:
    """Update KEY=... in place or append cleanly without disturbing comments."""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    new_line = f"{key}={value}"

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = []

    updated = False
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if pattern.match(line):
            lines[idx] = new_line
            updated = True
            break

    if not updated:
        lines.append(new_line)

    content = "\n".join(lines) + "\n"
    env_path.write_text(content, encoding="utf-8")


async def _run_docker_command(*args: str) -> str | None:
    """Run a short-lived Docker CLI command asynchronously, returning stdout or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=_DOCKER_CMD_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, asyncio.TimeoutError):
        return None

    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace")


async def _inspect_docker_waha_api_key() -> str | None:
    """Inspect a running local WAHA Docker container for its active API key."""
    ps_output = await _run_docker_command("ps", "--format", "{{.ID}}\t{{.Image}}")
    if not ps_output:
        return None

    container_id: str | None = None
    for line in ps_output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and "waha" in parts[1].lower():
            container_id = parts[0].strip()
            break

    if not container_id:
        return None

    inspect_output = await _run_docker_command(
        "inspect", "--format", "{{json .Config.Env}}", container_id
    )
    if not inspect_output:
        return None

    try:
        env_entries = json.loads(inspect_output.strip())
    except json.JSONDecodeError:
        return None

    if not isinstance(env_entries, list):
        return None

    env_map: dict[str, str] = {}
    for entry in env_entries:
        if isinstance(entry, str) and "=" in entry:
            k, v = entry.split("=", 1)
            env_map[k] = v

    for candidate in ("WAHA_API_KEY", "WHATSAPP_API_KEY"):
        val = (env_map.get(candidate) or "").strip()
        if val:
            return val
    return None


async def ensure_proxy_and_waha_secrets(
    env_path: Path = Path(".env"),
    waha_base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """Ensure PROXY_API_KEY, WAHA_API_KEY, and WAHA_WEBHOOK_SECRET exist in .env."""
    ensure_env_file(env_path=env_path)
    values = read_env_dict(env_path=env_path)

    proxy_key = values.get("PROXY_API_KEY", "").strip()
    if proxy_key.lower() in _PLACEHOLDER_PROXY_KEYS:
        proxy_key = f"sk-instinct-{secrets.token_hex(16)}"
        upsert_env_var("PROXY_API_KEY", proxy_key, env_path=env_path)

    api_key = values.get("WAHA_API_KEY", "").strip()
    if not api_key:
        api_key = secrets.token_hex(16)
        upsert_env_var("WAHA_API_KEY", api_key, env_path=env_path)

    webhook_secret = values.get("WAHA_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        webhook_secret = secrets.token_hex(16)
        upsert_env_var("WAHA_WEBHOOK_SECRET", webhook_secret, env_path=env_path)

    target_url = (
        waha_base_url or values.get("WAHA_BASE_URL") or "http://localhost:3000"
    )
    target_url = str(target_url).strip().rstrip("/")
    if target_url:
        probe_url = f"{target_url}/api/sessions"
        headers = {"X-Api-Key": api_key}
        try:
            if client is not None:
                response = await client.get(probe_url, headers=headers)
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(_WAHA_PROBE_TIMEOUT_SECONDS)
                ) as probe_client:
                    response = await probe_client.get(probe_url, headers=headers)

            if response.status_code == 401:
                docker_key = await _inspect_docker_waha_api_key()
                if docker_key:
                    api_key = docker_key
                    upsert_env_var("WAHA_API_KEY", api_key, env_path=env_path)
                    logger.info(
                        "Synced active WAHA_API_KEY from running Docker WAHA container."
                    )
        except httpx.HTTPError:
            pass

    return {
        "PROXY_API_KEY": proxy_key,
        "WAHA_API_KEY": api_key,
        "WAHA_WEBHOOK_SECRET": webhook_secret,
    }


async def ensure_waha_secrets(
    env_path: Path = Path(".env"),
    waha_base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """Backwards-compatible wrapper returning WAHA_API_KEY and WAHA_WEBHOOK_SECRET."""
    all_secrets = await ensure_proxy_and_waha_secrets(
        env_path=env_path,
        waha_base_url=waha_base_url,
        client=client,
    )
    return {
        "WAHA_API_KEY": all_secrets["WAHA_API_KEY"],
        "WAHA_WEBHOOK_SECRET": all_secrets["WAHA_WEBHOOK_SECRET"],
    }
