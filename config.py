"""Application configuration loaded from environment variables and .env."""

from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for instinct-openai-proxy."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    ACTIVE_PLATFORM: Literal["TELEGRAM", "WHATSAPP"] = "TELEGRAM"

    # Telegram MTProto credentials
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    TG_PHONE_NUMBER: str = ""
    INSTINCT_TG_USERNAME: str = "@instinct_telegram_bot"

    # WhatsApp (WAHA) credentials and target chat
    WAHA_BASE_URL: str = "http://localhost:3000"
    WAHA_API_KEY: str = ""
    WAHA_SESSION: str = "default"
    WAHA_WEBHOOK_SECRET: str = ""
    WAHA_WEBHOOK_URL: str = "http://host.docker.internal:8000/webhooks/whatsapp"
    WAHA_AUTO_CONFIGURE: bool = True
    INSTINCT_WA_NUMBER: str = "16508702892@c.us"

    # API security
    PROXY_API_KEY: str = ""

    # Session, timing, and server tuning
    TG_SESSION_NAME: str = "instinct_session"
    TG_SESSION_DIR: str = ""
    REPLY_SETTLE_SECONDS: float = Field(
        default=1.8,
        validation_alias=AliasChoices(
            "REPLY_SETTLE_SECONDS", "RESPONSE_SETTLE_SECONDS"
        ),
    )
    RESPONSE_TIMEOUT_SECONDS: float = 60.0
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    @field_validator("ACTIVE_PLATFORM", mode="before")
    @classmethod
    def _normalize_active_platform(cls, value: Any) -> str:
        if value is None:
            return "TELEGRAM"
        cleaned = str(value).strip().upper()
        if not cleaned:
            return "TELEGRAM"
        if cleaned not in {"TELEGRAM", "WHATSAPP"}:
            raise ValueError(
                f"ACTIVE_PLATFORM must be 'TELEGRAM' or 'WHATSAPP', got {value!r}."
            )
        return cleaned

    @field_validator("TG_API_ID", mode="before")
    @classmethod
    def _coerce_tg_api_id(cls, value: Any) -> int:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0
        return int(value)

    @field_validator("WAHA_BASE_URL", mode="before")
    @classmethod
    def _normalize_waha_base_url(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip().rstrip("/")

    @field_validator(
        "TG_API_HASH",
        "TG_PHONE_NUMBER",
        "INSTINCT_TG_USERNAME",
        "WAHA_API_KEY",
        "WAHA_SESSION",
        "WAHA_WEBHOOK_SECRET",
        "WAHA_WEBHOOK_URL",
        "INSTINCT_WA_NUMBER",
        "PROXY_API_KEY",
        "TG_SESSION_NAME",
        "TG_SESSION_DIR",
        "LOG_LEVEL",
        mode="before",
    )
    @classmethod
    def _strip_strings(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def tg_session_path(self) -> str:
        """Return the Telethon session path, creating TG_SESSION_DIR if configured."""
        if self.TG_SESSION_DIR:
            session_dir = Path(self.TG_SESSION_DIR)
            session_dir.mkdir(parents=True, exist_ok=True)
            return str(session_dir / self.TG_SESSION_NAME)
        return self.TG_SESSION_NAME

    def validate_telegram_config(self, require_phone: bool = True) -> None:
        """Ensure required Telegram credentials are present before connecting."""
        missing: list[str] = []
        if self.TG_API_ID <= 0:
            missing.append("TG_API_ID")
        if not self.TG_API_HASH:
            missing.append("TG_API_HASH")
        if require_phone and not self.TG_PHONE_NUMBER:
            missing.append("TG_PHONE_NUMBER")
        if not self.INSTINCT_TG_USERNAME:
            missing.append("INSTINCT_TG_USERNAME")

        if missing:
            joined = ", ".join(missing)
            raise ValueError(
                f"Missing required Telegram configuration in .env: {joined}"
            )

    def validate_whatsapp_config(self) -> None:
        """Ensure required WhatsApp (WAHA) settings are configured before startup."""
        missing: list[str] = []
        if not self.WAHA_BASE_URL:
            missing.append("WAHA_BASE_URL")
        if not self.WAHA_SESSION:
            missing.append("WAHA_SESSION")
        if not self.INSTINCT_WA_NUMBER or "X" in self.INSTINCT_WA_NUMBER:
            missing.append("INSTINCT_WA_NUMBER")

        if missing:
            joined = ", ".join(missing)
            raise ValueError(
                f"Missing or placeholder WhatsApp configuration in .env: {joined}"
            )


settings = Settings()
