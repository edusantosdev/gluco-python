"""Thin synchronous HTTP client for the local daemon.

Kept deliberately small — the frontend should never speak Abbott's API
directly, and it should never need anything more than what's exposed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class DaemonUnreachable(RuntimeError):
    """Raised when the daemon doesn't respond at all."""


class DaemonError(RuntimeError):
    """Raised on a 5xx (other than 503-starting) from the daemon."""


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

    def close(self) -> None:
        self._client.close()

    def current(self) -> CurrentReading:
        try:
            resp = self._client.get("/glucose/current")
        except httpx.HTTPError as e:
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
        resp = self._client.post("/auth/refresh")
        resp.raise_for_status()
        return resp.json()
