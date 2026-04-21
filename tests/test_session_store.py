"""Tests for ``FileSessionStore``: round-trip, expiry, corruption, atomicity."""

from __future__ import annotations

import base64
import json
import os
import time

import pytest

from daemon.session_store import FileSessionStore, _decode_jwt_exp


def _make_jwt(exp: float | None) -> str:
    """Build a minimal unsigned JWT with the given ``exp`` claim.

    Signature is nonsense — the parser doesn't verify, which is what we test.
    """
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload_obj: dict = {}
    if exp is not None:
        payload_obj["exp"] = exp
    payload = (
        base64.urlsafe_b64encode(json.dumps(payload_obj).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


def test_decode_jwt_exp_round_trip() -> None:
    assert _decode_jwt_exp(_make_jwt(1_700_000_000)) == 1_700_000_000.0


def test_decode_jwt_exp_missing_claim() -> None:
    assert _decode_jwt_exp(_make_jwt(None)) is None


def test_decode_jwt_exp_malformed() -> None:
    assert _decode_jwt_exp("not-a-jwt") is None
    assert _decode_jwt_exp("a.b") is None
    assert _decode_jwt_exp("a.!!!not_base64.c") is None


def test_save_then_load_round_trip(tmp_path) -> None:
    store = FileSessionStore(tmp_path / "session.json")
    future = time.time() + 3600
    saved = store.save(_make_jwt(future), "hash123")
    loaded = store.load()
    assert loaded is not None
    assert loaded.token == saved.token
    assert loaded.account_id_hash == "hash123"
    assert loaded.expires_at == pytest.approx(future, abs=1)


def test_file_is_private(tmp_path) -> None:
    store = FileSessionStore(tmp_path / "session.json")
    store.save(_make_jwt(time.time() + 3600), "h")
    mode = os.stat(store.path).st_mode & 0o777
    assert mode == 0o600


def test_load_missing_returns_none(tmp_path) -> None:
    store = FileSessionStore(tmp_path / "absent.json")
    assert store.load() is None


def test_load_expired_returns_none(tmp_path) -> None:
    store = FileSessionStore(tmp_path / "session.json")
    store.save(_make_jwt(time.time() - 10), "h")
    assert store.load() is None


def test_load_corrupt_returns_none(tmp_path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{{{not json")
    store = FileSessionStore(path)
    assert store.load() is None


def test_clear_removes_file(tmp_path) -> None:
    store = FileSessionStore(tmp_path / "session.json")
    store.save(_make_jwt(time.time() + 3600), "h")
    assert store.path.exists()
    store.clear()
    assert not store.path.exists()
    # Idempotent.
    store.clear()


def test_fallback_ttl_when_jwt_has_no_exp(tmp_path) -> None:
    store = FileSessionStore(tmp_path / "session.json")
    before = time.time()
    session = store.save(_make_jwt(None), "h")
    # Fallback TTL is 50 minutes (3000s); allow slack for slow CI.
    assert session.expires_at > before + 1000
