"""Settings loaded from environment variables (Railway variables or local .env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./dentalux_outreach.db"


class Settings(BaseSettings):
    """All configuration comes from the environment. Secrets are SecretStr so they never show up in repr/logs."""

    # env_ignore_empty: a variable that is set but blank (e.g. "DAILY_SEND_LIMIT=" copied from .env.example)
    # falls back to the default instead of failing validation.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    # Telegram: the approval bot itself (NOT the DentaLux clinic bot).
    bot_token: SecretStr | None = None
    # Telegram numeric user id of the owner. Only this user may use the bot.
    admin_user_id: int = 0

    database_url: str = DEFAULT_DATABASE_URL

    # Optional: without a key the bot uses built-in templates.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"

    daily_send_limit: int = Field(default=10, ge=0)
    send_pause_seconds: int = Field(default=180, ge=0)

    # Day boundaries for the daily limit.
    timezone: str = "Asia/Tashkent"

    # Webhook mode (recommended on Railway). Empty = long polling (local testing only).
    public_webhook_url: str | None = None
    # Sent to Telegram as secret_token; Telegram echoes it back in X-Telegram-Bot-Api-Secret-Token.
    webhook_secret: SecretStr | None = None
    # Long polling is for local testing only. Production must use the webhook.
    allow_polling: bool = False

    # Railway injects PORT.
    port: int = Field(default=8080, ge=1, le=65535)

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: object) -> object:
        """Railway gives postgres:// or postgresql://; SQLAlchemy async needs the asyncpg driver."""
        if isinstance(value, str):
            if value.startswith("postgres://"):
                return "postgresql+asyncpg://" + value[len("postgres://") :]
            if value.startswith("postgresql://"):
                return "postgresql+asyncpg://" + value[len("postgresql://") :]
        return value

    @field_validator("public_webhook_url", mode="before")
    @classmethod
    def _strip_trailing_slash(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().rstrip("/")
            return value or None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
