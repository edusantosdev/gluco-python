"""Credentials resolver — Keychain first, environment/.env as fallback.

Why a shared module
-------------------
The daemon reads creds at startup, the ``set_credentials.py`` helper
writes them, and the preferences UI updates them. Keeping the Keychain
keys and lookup logic in one place means there is exactly one definition
of "where does the email live?" — no way to typo a service name in one
spot and silently read a stale value in another.

Storage layout
--------------
Keyring uses a ``(service, username)`` key. We use a single service name
and two synthetic usernames so both secrets live in the same
Keychain-visible group:

    service  = "librelinkup-glucose-menubar"
    username = "email"     → the account email (not secret, but kept here
                             so a single Keychain entry covers everything)
    username = "password"  → the LibreLinkUp password

If the Keychain is unreadable (no GUI session, CI, remote SSH without
`security unlock-keychain`), we fall back to environment variables so
development and headless usage both keep working. That matches the
plan's guidance: Keychain first, ``.env`` fallback, same code path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "librelinkup-glucose-menubar"
_EMAIL_KEY = "email"
_PASSWORD_KEY = "password"


class KeyringLike(Protocol):
    """Subset of the ``keyring`` module surface we depend on.

    Lets tests swap in a fake without patching the real Keychain.
    """

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _default_keyring() -> KeyringLike | None:
    """Import ``keyring`` lazily so the dep stays optional in constrained envs."""
    try:
        import keyring  # type: ignore[import-not-found]

        return keyring
    except Exception as e:  # noqa: BLE001 — missing/broken backends are non-fatal
        log.debug("keyring unavailable: %s", e)
        return None


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    source: str  # "keychain" | "env" — useful for logs and prefs UI


def load_credentials(
    env_email: str | None,
    env_password: str | None,
    *,
    keyring_module: KeyringLike | None = None,
) -> Credentials | None:
    """Return the best available credentials, or ``None`` if none exist.

    Precedence (from plan §10):
        1. Keychain (via ``keyring``) — production path
        2. Env / ``.env`` — dev + headless fallback

    We deliberately don't mix sources within a single call (e.g. email
    from Keychain + password from env). If the Keychain has one of the
    two but not both, we treat it as absent and fall through to env.
    A partially-seeded Keychain is always a bug, and silently combining
    would make that bug invisible.
    """
    kr = keyring_module if keyring_module is not None else _default_keyring()
    if kr is not None:
        try:
            kc_email = kr.get_password(KEYCHAIN_SERVICE, _EMAIL_KEY)
            kc_password = kr.get_password(KEYCHAIN_SERVICE, _PASSWORD_KEY)
        except Exception as e:  # noqa: BLE001 — locked keychain etc.
            log.warning("Keychain read failed; falling back to env: %s", e)
            kc_email = kc_password = None
        if kc_email and kc_password:
            return Credentials(email=kc_email, password=kc_password, source="keychain")

    if env_email and env_password:
        return Credentials(email=env_email, password=env_password, source="env")
    return None


def save_credentials(
    email: str,
    password: str,
    *,
    keyring_module: KeyringLike | None = None,
) -> None:
    """Write ``email``/``password`` to Keychain. Raises on failure."""
    if not email or not password:
        raise ValueError("Both email and password are required")
    kr = keyring_module if keyring_module is not None else _default_keyring()
    if kr is None:
        raise RuntimeError(
            "keyring is not available — install it or edit .env instead"
        )
    kr.set_password(KEYCHAIN_SERVICE, _EMAIL_KEY, email)
    kr.set_password(KEYCHAIN_SERVICE, _PASSWORD_KEY, password)


def clear_credentials(*, keyring_module: KeyringLike | None = None) -> None:
    """Remove both entries. Idempotent — missing entries are not errors."""
    kr = keyring_module if keyring_module is not None else _default_keyring()
    if kr is None:
        return
    for key in (_EMAIL_KEY, _PASSWORD_KEY):
        try:
            kr.delete_password(KEYCHAIN_SERVICE, key)
        except Exception:  # noqa: BLE001 — already-absent is fine
            pass
