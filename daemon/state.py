"""Daemon state machine — single source of truth that the API exposes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class Status(str, Enum):
    """Discriminated states the frontend can rely on without parsing strings.

    Inheriting from ``str`` makes the enum JSON-serialisable as the value
    string with no extra encoder work."""

    STARTING = "starting"  # daemon up, no reading yet
    OK = "ok"  # fresh reading, sensor talking
    STALE = "stale"  # last reading older than the staleness window
    WARMING_UP = "warming_up"  # sensor in 60-min warm-up (Phase 3 detection)
    SENSOR_ENDED = "sensor_ended"  # sensor expired (Phase 3 detection)
    NO_NETWORK = "no_network"  # connection failures, backing off
    AUTH_FAILED = "auth_failed"  # credentials rejected
    TOS_REQUIRED = "tos_required"  # user must accept in Libre app
    RATE_LIMITED = "rate_limited"  # honoring retry_after


@dataclass
class DaemonState:
    """Cache of the latest known reading plus the daemon's view of itself."""

    status: Status = Status.STARTING
    reading: object | None = None  # GlucoseMeasurementWithTrend at runtime
    fetched_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def http_status_summary(s: Status) -> str:
    """Three-bucket summary for ``/health`` consumers that just want a colour."""
    if s == Status.OK:
        return "ok"
    if s in (
        Status.STARTING,
        Status.WARMING_UP,
        Status.STALE,
        Status.RATE_LIMITED,
        Status.NO_NETWORK,
    ):
        return "degraded"
    return "down"  # AUTH_FAILED, TOS_REQUIRED, SENSOR_ENDED
