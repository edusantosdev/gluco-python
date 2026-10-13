"""Tests for :mod:`daemon.local_guard`."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from daemon.local_guard import LocalOnlyGuard

PORT = 8765


def _client(bind_host: str = "127.0.0.1") -> TestClient:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/auth/refresh")
    def refresh() -> dict:
        return {"refreshed": True}

    app.add_middleware(LocalOnlyGuard, port=PORT, bind_host=bind_host)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}")


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "LOCALHOST"])
def test_local_clients_are_allowed(host: str) -> None:
    # httpx / urllib / curl: loopback Host, no Origin, no Sec-Fetch-Site.
    c = _client()
    assert c.get("/health", headers={"host": host}).status_code == 200
    assert c.post("/auth/refresh", headers={"host": host}).status_code == 200


def test_same_origin_browser_requests_are_allowed() -> None:
    # e.g. the FastAPI /docs page served by the daemon itself.
    c = _client()
    headers = {"origin": f"http://127.0.0.1:{PORT}", "sec-fetch-site": "same-origin"}
    assert c.post("/auth/refresh", headers=headers).status_code == 200
    assert c.get("/health", headers={"sec-fetch-site": "none"}).status_code == 200


def test_dns_rebinding_host_is_rejected() -> None:
    c = _client()
    resp = c.get("/health", headers={"host": f"evil.example:{PORT}"})
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "null",  # sandboxed iframe / file://
        "http://localhost:3000",  # another local dev server
        f"https://127.0.0.1:{PORT}",  # wrong scheme
        "http://127.0.0.1:notaport",
    ],
)
def test_foreign_origins_are_rejected(origin: str) -> None:
    c = _client()
    resp = c.post("/auth/refresh", headers={"origin": origin})
    assert resp.status_code == 403


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_fetch_without_origin_is_rejected(site: str) -> None:
    c = _client()
    assert c.get("/health", headers={"sec-fetch-site": site}).status_code == 403


def test_custom_bind_host_is_allowed_but_wildcard_is_not_trusted() -> None:
    assert _client("192.168.1.5").get(
        "/health", headers={"host": "192.168.1.5:8765"}
    ).status_code == 200
    assert _client("0.0.0.0").get(
        "/health", headers={"host": "0.0.0.0:8765"}
    ).status_code == 403
