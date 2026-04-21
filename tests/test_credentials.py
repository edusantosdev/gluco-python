"""Credentials precedence + failure modes for ``glucose_common.credentials``."""

from __future__ import annotations

import pytest

from glucose_common.credentials import (
    KEYCHAIN_SERVICE,
    clear_credentials,
    load_credentials,
    save_credentials,
)


class FakeKeyring:
    """In-memory stand-in for the ``keyring`` module."""

    def __init__(self, *, get_raises: Exception | None = None) -> None:
        self._store: dict[tuple[str, str], str] = {}
        self._get_raises = get_raises

    def get_password(self, service: str, username: str) -> str | None:
        if self._get_raises is not None:
            raise self._get_raises
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)


def test_keychain_takes_precedence_over_env() -> None:
    kr = FakeKeyring()
    save_credentials("k@x", "kpw", keyring_module=kr)
    creds = load_credentials(
        env_email="env@x", env_password="envpw", keyring_module=kr
    )
    assert creds is not None
    assert creds.email == "k@x"
    assert creds.password == "kpw"
    assert creds.source == "keychain"


def test_env_used_when_keychain_empty() -> None:
    kr = FakeKeyring()
    creds = load_credentials(env_email="e@x", env_password="p", keyring_module=kr)
    assert creds is not None
    assert creds.source == "env"
    assert creds.email == "e@x"


def test_none_when_both_sources_empty() -> None:
    kr = FakeKeyring()
    assert load_credentials(env_email=None, env_password=None, keyring_module=kr) is None
    assert load_credentials(env_email="", env_password="", keyring_module=kr) is None


def test_partial_keychain_falls_back_to_env() -> None:
    """A half-seeded Keychain must not be mixed with env values."""
    kr = FakeKeyring()
    kr.set_password(KEYCHAIN_SERVICE, "email", "k@x")  # password missing
    creds = load_credentials(
        env_email="env@x", env_password="envpw", keyring_module=kr
    )
    assert creds is not None
    assert creds.source == "env"
    assert creds.email == "env@x"


def test_keychain_exception_logs_and_falls_back() -> None:
    kr = FakeKeyring(get_raises=RuntimeError("keychain locked"))
    creds = load_credentials(env_email="e@x", env_password="p", keyring_module=kr)
    assert creds is not None
    assert creds.source == "env"


def test_save_rejects_empty_values() -> None:
    kr = FakeKeyring()
    with pytest.raises(ValueError):
        save_credentials("", "p", keyring_module=kr)
    with pytest.raises(ValueError):
        save_credentials("e", "", keyring_module=kr)


def test_clear_is_idempotent() -> None:
    kr = FakeKeyring()
    clear_credentials(keyring_module=kr)  # no entries → silent no-op
    save_credentials("e@x", "p", keyring_module=kr)
    clear_credentials(keyring_module=kr)
    assert load_credentials(env_email=None, env_password=None, keyring_module=kr) is None


def test_save_then_clear_roundtrip() -> None:
    kr = FakeKeyring()
    save_credentials("e@x", "p", keyring_module=kr)
    assert load_credentials(env_email=None, env_password=None, keyring_module=kr) is not None
    clear_credentials(keyring_module=kr)
    assert load_credentials(env_email=None, env_password=None, keyring_module=kr) is None
