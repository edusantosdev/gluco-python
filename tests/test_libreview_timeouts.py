"""Tests for :mod:`daemon.libreview_timeouts`."""

from __future__ import annotations

import socket
import time

import pylibrelinkup.pylibrelinkup as llu_module
import pytest
import requests
from pylibrelinkup import APIUrl, PyLibreLinkUp

from daemon import libreview_timeouts
from daemon.service import _is_network_failure


@pytest.fixture
def unpatched(monkeypatch):
    """Start each test from the real module; monkeypatch restores it afterwards."""
    monkeypatch.setattr(llu_module, "requests", requests)


@pytest.fixture
def silent_server():
    """A TCP listener that completes the handshake but never answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


def test_install_adds_default_timeout(unpatched, monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: calls.append(kw))

    assert libreview_timeouts.install(timeout=(1.0, 2.0)) is True
    llu_module.requests.get(url="http://x/a", headers={})
    llu_module.requests.post(url="http://x/b", json={}, timeout=9)

    assert calls[0]["timeout"] == (1.0, 2.0)
    assert calls[1]["timeout"] == 9  # an explicit timeout wins


def test_install_is_scoped_and_idempotent(unpatched) -> None:
    assert libreview_timeouts.install()
    wrapped = llu_module.requests
    assert libreview_timeouts.install()
    assert llu_module.requests is wrapped
    # Only the library's reference is wrapped; the real module is untouched.
    assert requests.get.__module__ == "requests.api"
    # Non-wrapped attributes (exceptions etc.) pass straight through.
    assert wrapped.HTTPError is requests.HTTPError


@pytest.mark.parametrize("call", ["get_patients", "authenticate"])
def test_hung_server_times_out_instead_of_blocking(unpatched, silent_server, call) -> None:
    libreview_timeouts.install(timeout=(1.0, 0.5))
    client = PyLibreLinkUp(email="a@b.c", password="x", api_url=APIUrl.US)
    client.api_url = silent_server
    client.token = "t"  # get_patients is not behind @authenticated, but be safe

    start = time.monotonic()
    with pytest.raises(requests.Timeout) as exc_info:
        getattr(client, call)()
    assert time.monotonic() - start < 5
    # The poller reports this as a connectivity problem, not a generic error.
    assert _is_network_failure(exc_info.value)
