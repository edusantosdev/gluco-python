"""Pydantic response schemas for the daemon's HTTP API.

Centralized so they can be re-exported as the OpenAPI schema and reused
by future SDKs (Swift frontend etc.) without duplication.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .state import Status


class HealthResponse(BaseModel):
    status: str = Field(..., description="Three-bucket summary: ok | degraded | down")
    daemon_status: Status
    uptime_seconds: int
    last_error: str | None = None
    consecutive_failures: int = 0


class CurrentResponse(BaseModel):
    status: Status
    unit: str | None = None

    value: float | None = None
    value_in_mg_per_dl: float | None = None
    trend: str | None = None  # falling_fast | falling | stable | rising | rising_fast
    trend_symbol: str | None = None  # ↓ ↘ → ↗ ↑
    sensor_timestamp: datetime | None = None
    fetched_at: datetime | None = None
    reading_age_seconds: int | None = None
    fetch_age_seconds: int | None = None
    is_high: bool | None = None
    is_low: bool | None = None


class GraphPoint(BaseModel):
    value: float
    timestamp: datetime


class PatientResponse(BaseModel):
    first_name: str
    last_name: str
    patient_id: str | None = None
