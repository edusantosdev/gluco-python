"""Thin synchronous HTTP client for the local daemon.

Kept deliberately small — the frontend should never speak Abbott's API
directly, and it should never need anything more than what's exposed here.
"""

from __future__ import annotations

import errno
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


class DaemonUnreachable(RuntimeError):
    """Raised when the daemon doesn't respond at all."""


class DaemonNotReady(DaemonUnreachable):
    """Raised when nothing is listening on the daemon port yet (typ. still booting)."""


class DaemonError(RuntimeError):
    """Raised on a 5xx (other than 503-starting) from the daemon."""


def _exception_chain_has_econnrefused(exc: BaseException) -> bool:
    """True if ``exc`` carries a refused TCP connect (cause chain or nested args).

    ``httpcore`` wraps ``ConnectionRefusedError`` as ``ConnectError.args[0]``;
    ``__cause__`` alone does not always reach it.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ConnectionRefusedError):
            return True
        if isinstance(cur, OSError) and cur.errno == errno.ECONNREFUSED:
            return True
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)
        for arg in getattr(cur, "args", ()):
            if isinstance(arg, BaseException):
                stack.append(arg)
    return False


def _transport_is_daemon_socket_not_ready(exc: httpx.HTTPError) -> bool:
    """Connection failed before an HTTP response — usual race right after login/install."""
    return isinstance(exc, httpx.ConnectError) and _exception_chain_has_econnrefused(exc)


@dataclass
class CurrentReading:
    status: str
    value: float | None
    value_in_mg_per_dl: float | None
    trend_symbol: str | None
    unit: str | None
    sensor_timestamp: str | None
    reading_age_seconds: int | None
    fetch_age_seconds: int | None
    last_error: str | None = None

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> CurrentReading:
        return cls(
            status=data.get("status", "unknown"),
            value=data.get("value"),
            value_in_mg_per_dl=data.get("value_in_mg_per_dl"),
            trend_symbol=data.get("trend_symbol"),
            unit=data.get("unit"),
            sensor_timestamp=data.get("sensor_timestamp"),
            reading_age_seconds=data.get("reading_age_seconds"),
            fetch_age_seconds=data.get("fetch_age_seconds"),
        )


class DaemonClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout: float = 5.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._client.close()

    def current(self) -> CurrentReading:
        try:
            with self._lock:
                resp = self._client.get("/glucose/current")
        except httpx.HTTPError as e:
            if _transport_is_daemon_socket_not_ready(e):
                raise DaemonNotReady(str(e)) from e
            raise DaemonUnreachable(str(e)) from e
        if resp.status_code == 503:
            # "Daemon starting" — treat as a transient state, not an error.
            return CurrentReading(
                status="starting",
                value=None,
                value_in_mg_per_dl=None,
                trend_symbol=None,
                unit=None,
                sensor_timestamp=None,
                reading_age_seconds=None,
                fetch_age_seconds=None,
                last_error=None,
            )
        if resp.status_code >= 500:
            raise DaemonError(f"{resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()
        return CurrentReading.from_payload(resp.json())

    def force_refresh(self) -> dict[str, Any]:
        with self._lock:
            resp = self._client.post("/auth/refresh")
        resp.raise_for_status()
        return resp.json()

    def graph(self, hours: int = 8) -> list[GraphPoint]:
        """Fetch cached historical readings from the daemon (oldest first).

        Returns ``[]`` if the daemon hasn't completed a successful poll yet.
        Raises ``DaemonUnreachable`` / ``DaemonNotReady`` on transport errors
        and ``DaemonError`` on 5xx responses, matching the ``current()``
        contract.
        """
        try:
            with self._lock:
                resp = self._client.get("/glucose/graph", params={"hours": hours})
        except httpx.HTTPError as e:
            if _transport_is_daemon_socket_not_ready(e):
                raise DaemonNotReady(str(e)) from e
            raise DaemonUnreachable(str(e)) from e
        if resp.status_code >= 500:
            raise DaemonError(f"{resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()
        out: list[GraphPoint] = []
        for item in resp.json():
            ts_raw = item.get("timestamp")
            value = item.get("value")
            if ts_raw is None or value is None:
                continue
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            out.append(GraphPoint(value_mg_dl=float(value), timestamp=ts))
        return out


@dataclass
class GraphPoint:
    value_mg_dl: float
    timestamp: datetime  # tz-aware UTC
