"""Typed settings for the daemon.

Reads non-credential settings from environment variables (and ``.env``
if present). Credentials are read exclusively from the macOS Keychain
via :mod:`glucose_common.credentials` — there is no env fallback. If the
Keychain has no entries on startup, the daemon exits cleanly and waits
for the menubar frontend to prompt the user and seed them.
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
from glucose_common.regions import guess_libreview_region  # noqa: E402

_POLL_INTERVAL_SECONDS = 60
_STALE_AFTER_SECONDS = 360


def _default_state_dir() -> Path:
    return Path.home() / ".glucobar"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Populated by ``load_settings()`` from the Keychain. Not sourced
    # from env so a stray ``LIBREVIEW_*`` variable in someone's shell
    # can't override the Keychain value.
    libreview_email: str | None = Field(default=None)
    libreview_password: SecretStr | None = Field(default=None)
    # If unset, infer from locale (e.g. ``pt_PT`` -> ``PT`` -> EU cluster)
    # and fall back to US. Explicit env/.env still wins.
    libreview_region: str = Field(
        default_factory=guess_libreview_region,
        alias="LIBREVIEW_REGION",
    )

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

    @property
    def poll_interval_seconds(self) -> int:
        # Fixed daemon cadence; no env override.
        return _POLL_INTERVAL_SECONDS

    @property
    def stale_after_seconds(self) -> int:
        # Reading is "stale" when older than 6 minutes.
        return _STALE_AFTER_SECONDS


class MissingCredentialsError(RuntimeError):
    """The Keychain has no LibreView credentials yet."""


def load_settings() -> Settings:
    """Instantiate ``Settings`` and load credentials from the Keychain.

    Kept as a function so tests can monkeypatch ``load_credentials`` and
    the environment independently."""
    s = Settings()  # type: ignore[call-arg]  — pulled from env at runtime

    resolved = load_credentials()
    if resolved is None:
        raise MissingCredentialsError(
            "No LibreView credentials found in the macOS Keychain. "
            "Start the menubar app to be prompted on first run, or run "
            "`python scripts/set_credentials.py` to save them manually."
        )

    s.libreview_email = resolved.email
    s.libreview_password = SecretStr(resolved.password)

    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s
