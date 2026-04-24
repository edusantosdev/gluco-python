"""Credentials resolver — macOS Keychain with supervised-injection fallback.

Why a shared module
-------------------
The daemon reads creds at startup, the ``set_credentials.py`` helper
writes them, and the menubar preferences UI updates them. Keeping the
Keychain keys and lookup logic in one place means there is exactly one
definition of "where does the email live?" — no way to typo a service
name in one spot and silently read a stale value in another.

Storage layout
--------------
Keyring uses a ``(service, username)`` key. We use a single service name
and two synthetic usernames so both secrets live in the same
Keychain-visible group:

    service  = "glucobar"
    username = "email"     → the account email (not secret, but kept here
                             so a single Keychain entry covers everything)
    username = "password"  → the LibreView password

Source-of-truth ordering
------------------------
The daemon prefers credentials passed in its environment (set by the
menubar supervisor) over a direct Keychain read. This avoids a second
Keychain authorisation dialog for the daemon subprocess: the frontend
binary (``GlucoBar``) is the only process that ever touches Keychain, and
hands credentials to the daemon child via env. Direct ``python -m daemon``
dev launches still fall back to Keychain when env vars are absent.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "glucobar"
_EMAIL_KEY = "email"
_PASSWORD_KEY = "password"

ENV_EMAIL = "GLUCOBAR_LIBREVIEW_EMAIL"
ENV_PASSWORD = "GLUCOBAR_LIBREVIEW_PASSWORD"  # noqa: S105 — env-var name, not a password


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


def load_credentials_from_env(
    env: dict[str, str] | None = None,
) -> Credentials | None:
    """Return credentials from ``GLUCOBAR_LIBREVIEW_*`` env vars, if both set.

    Used by the daemon when spawned as a child of the supervised menubar
    frontend: the frontend reads Keychain once (triggering at most one
    macOS dialog) and passes credentials down through env, so the daemon
    subprocess never has to touch Keychain itself.
    """
    src = env if env is not None else os.environ
    email = src.get(ENV_EMAIL, "").strip()
    password = src.get(ENV_PASSWORD, "")
    if email and password:
        return Credentials(email=email, password=password)
    return None


def load_credentials(
    *,
    keyring_module: KeyringLike | None = None,
    env: dict[str, str] | None = None,
) -> Credentials | None:
    """Return the stored credentials, or ``None`` if none are available.

    Resolution order:

    1. ``GLUCOBAR_LIBREVIEW_EMAIL`` / ``GLUCOBAR_LIBREVIEW_PASSWORD`` env
       vars — used by the frontend supervisor to hand credentials to the
       daemon without a second Keychain prompt.
    2. macOS Keychain via ``keyring``.

    Both halves (email + password) must be present at the resolved
    source; a partially-seeded Keychain is always a bug, and silently
    returning half of it would make that bug invisible.
    """
    from_env = load_credentials_from_env(env=env)
    if from_env is not None:
        return from_env
    kr = keyring_module if keyring_module is not None else _default_keyring()
    if kr is None:
        return None
    try:
        kc_email = kr.get_password(KEYCHAIN_SERVICE, _EMAIL_KEY)
        kc_password = kr.get_password(KEYCHAIN_SERVICE, _PASSWORD_KEY)
    except Exception as e:  # noqa: BLE001 — locked keychain etc.
        log.warning("Keychain read failed: %s", e)
        return None
    if kc_email and kc_password:
        return Credentials(email=kc_email, password=kc_password)
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
            "keyring is not available — install the `keyring` Python package "
            "and ensure the macOS Keychain is reachable"
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
