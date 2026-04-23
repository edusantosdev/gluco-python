"""Typed settings for the daemon.

Reads from environment variables (and ``.env`` if present). Centralizing
config here means everything else in the daemon can ask for ``settings.X``
without doing its own env parsing.

Credentials
-----------
Email + password may come from either the macOS Keychain (via
``glucose_common.credentials``) or from ``.env``. To keep the same code
path across dev and production, we let pydantic ingest env vars freely
and then reconcile with Keychain in ``load_settings()`` — the resulting
``Settings`` always has a concrete email/password, whichever source they
came from.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from glucose_common.credentials import load_credentials  # noqa: E402


def _default_state_dir() -> Path:
    # Renamed from ``~/.glucose-display`` in v0.3.0. The daemon
    # recreates the directory on first run; any cached session will be
    # refreshed on the next LibreLinkUp call, so no explicit migration
    # is needed. Users who want to preserve rotating logs can
    # ``mv ~/.glucose-display ~/.glucobar`` before the first run.
    return Path.home() / ".glucobar"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Optional at the pydantic layer so a Keychain-only install can boot
    # without either variable being set. ``load_settings()`` fills them
    # before returning, and fails loudly if neither source has them.
    librelinkup_email: str | None = Field(default=None, alias="LIBRELINKUP_EMAIL")
    librelinkup_password: SecretStr | None = Field(default=None, alias="LIBRELINKUP_PASSWORD")
    librelinkup_region: str = Field(default="US", alias="LIBRELINKUP_REGION")

    # Reported back in logs so users can tell whether Keychain is active.
    credentials_source: str = Field(default="env")

    poll_interval_seconds: int = Field(default=60, alias="POLL_INTERVAL_SECONDS")

    # Reading is "stale" if the sensor timestamp is older than this many seconds.
    # FreeStyle Libre 3 pushes ~every minute; 6 minutes is generous but catches
    # genuine loss-of-signal.
    stale_after_seconds: int = Field(default=360, alias="STALE_AFTER_SECONDS")

    # HTTP server bind. 127.0.0.1 only — this daemon is not a public service.
    host: str = Field(default="127.0.0.1", alias="DAEMON_HOST")
    port: int = Field(default=8765, alias="DAEMON_PORT")

    # Where to put the lockfile, session store, and rotating log.
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


class MissingCredentialsError(RuntimeError):
    """Neither Keychain nor env supplied a full email+password pair."""


def load_settings() -> Settings:
    """Instantiate ``Settings`` and reconcile credentials with Keychain.

    Kept as a function so tests can monkeypatch ``load_credentials`` and
    the environment independently."""
    s = Settings()  # type: ignore[call-arg]  — pulled from env at runtime

    env_email = s.librelinkup_email
    env_password = s.librelinkup_password.get_secret_value() if s.librelinkup_password else None
    resolved = load_credentials(env_email=env_email, env_password=env_password)

    if resolved is None:
        raise MissingCredentialsError(
            "No LibreLinkUp credentials found. "
            "Either run `python scripts/set_credentials.py` to store them in "
            "macOS Keychain, or set LIBRELINKUP_EMAIL/LIBRELINKUP_PASSWORD in .env."
        )

    s.librelinkup_email = resolved.email
    s.librelinkup_password = SecretStr(resolved.password)
    s.credentials_source = resolved.source

    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s
