"""Typed settings for the daemon.

Reads from environment variables (and ``.env`` if present). Centralizing
config here means everything else in the daemon can ask for ``settings.X``
without doing its own env parsing.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_state_dir() -> Path:
    return Path.home() / ".glucose-display"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    librelinkup_email: str = Field(..., alias="LIBRELINKUP_EMAIL")
    librelinkup_password: SecretStr = Field(..., alias="LIBRELINKUP_PASSWORD")
    librelinkup_region: str = Field(default="US", alias="LIBRELINKUP_REGION")

    poll_interval_seconds: int = Field(default=60, alias="POLL_INTERVAL_SECONDS")

    # Reading is "stale" if the sensor timestamp is older than this many seconds.
    # FreeStyle Libre 3 pushes ~every minute; 6 minutes is generous but catches
    # genuine loss-of-signal.
    stale_after_seconds: int = Field(default=360, alias="STALE_AFTER_SECONDS")

    # HTTP server bind. 127.0.0.1 only — this daemon is not a public service.
    host: str = Field(default="127.0.0.1", alias="DAEMON_HOST")
    port: int = Field(default=8765, alias="DAEMON_PORT")

    # Where to put the lockfile and (Phase 3) the session store / logs.
    state_dir: Path = Field(default_factory=_default_state_dir, alias="DAEMON_STATE_DIR")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Backoff policy for network errors (seconds).
    # Starts at ``base``, doubles each failure, caps at ``cap``, with equal jitter.
    error_backoff_base_seconds: float = Field(
        default=10.0, alias="ERROR_BACKOFF_BASE_SECONDS"
    )
    error_backoff_cap_seconds: float = Field(
        default=300.0, alias="ERROR_BACKOFF_CAP_SECONDS"
    )

    # Backoff policy for rate-limit responses that don't carry Retry-After.
    ratelimit_backoff_base_seconds: float = Field(
        default=30.0, alias="RATELIMIT_BACKOFF_BASE_SECONDS"
    )
    ratelimit_backoff_cap_seconds: float = Field(
        default=600.0, alias="RATELIMIT_BACKOFF_CAP_SECONDS"
    )

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "daemon.lock"

    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.json"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "daemon.log"


def load_settings() -> Settings:
    """Instantiate ``Settings``. Kept as a function so tests can override env."""
    s = Settings()  # type: ignore[call-arg]  # pulled from env at runtime
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s
