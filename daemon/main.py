"""FastAPI app — exposes the daemon's state over localhost HTTP.

Run with::

    .venv/bin/python -m daemon            # convenient wrapper
    # or
    .venv/bin/uvicorn daemon.main:app --host 127.0.0.1 --port 8765

Always binds to ``127.0.0.1`` by default. This is a personal tool on a
trusted machine, not a service.
"""

from __future__ import annotations

import logging
import signal
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException

# Allow running as ``python -m daemon`` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from glucose_common.regions import resolve_librelinkup_region  # noqa: E402
from glucose_common.trend import trend_name, trend_symbol  # noqa: E402
from glucose_common.units import glucose_unit_label  # noqa: E402

from .backoff import ExponentialBackoff  # noqa: E402
from .config import MissingCredentialsError, Settings, load_settings  # noqa: E402
from .logging_setup import configure_logging  # noqa: E402
from .models import (  # noqa: E402
    CurrentResponse,
    GraphPoint,
    HealthResponse,
    PatientResponse,
)
from .service import GlucoseService  # noqa: E402
from .session_store import FileSessionStore  # noqa: E402
from .single_instance import acquire_lock  # noqa: E402
from .state import Status, http_status_summary  # noqa: E402

log = logging.getLogger("glucose.daemon")


def _build_service(settings: Settings) -> GlucoseService:
    region, region_hint = resolve_librelinkup_region(settings.librelinkup_region)
    if region_hint:
        log.info("%s", region_hint)
    log.info("Credentials loaded from %s", settings.credentials_source)
    # Safe by construction: ``load_settings`` raises if creds are missing.
    assert settings.librelinkup_email and settings.librelinkup_password
    return GlucoseService(
        email=settings.librelinkup_email,
        password=settings.librelinkup_password.get_secret_value(),
        region=region,
        poll_interval=settings.poll_interval_seconds,
        stale_after_seconds=settings.stale_after_seconds,
        session_store=FileSessionStore(settings.session_path),
        error_backoff=ExponentialBackoff(
            base_seconds=settings.error_backoff_base_seconds,
            cap_seconds=settings.error_backoff_cap_seconds,
        ),
        ratelimit_backoff=ExponentialBackoff(
            base_seconds=settings.ratelimit_backoff_base_seconds,
            cap_seconds=settings.ratelimit_backoff_cap_seconds,
        ),
    )


# Module-level so uvicorn can find ``daemon.main:app``. The single-instance
# lock is acquired here, not in lifespan, because by the time lifespan runs
# uvicorn has already bound the listening socket — and we want to bail out
# *before* that to keep error messages clean.
try:
    _settings = load_settings()
except MissingCredentialsError as _e:
    # Print to stderr + exit 78 (EX_CONFIG) so launchd logs stay readable
    # and ``launchctl print`` shows a distinct config-error exit code.
    print(f"glucose-daemon: {_e}", file=sys.stderr)
    sys.exit(78)
configure_logging(_settings.log_level, _settings.log_path)
acquire_lock(_settings.lock_path)
_service = _build_service(_settings)


def _install_signal_handlers() -> None:
    """Translate SIGTERM/SIGINT into an orderly poller shutdown.

    uvicorn already handles these at the HTTP layer, but our polling
    thread lives outside uvicorn's awareness. Chaining handlers keeps
    both reactions — stop accepting requests AND stop polling — which
    is what we want under ``launchctl stop`` or Ctrl-C.
    """
    prior = {}

    def _handler(signum, frame):  # noqa: ANN001
        log.info("Received %s; stopping poller", signal.Signals(signum).name)
        _service.stop()
        old = prior.get(signum)
        if callable(old):
            old(signum, frame)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prior[sig] = signal.getsignal(sig)
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # ``signal.signal`` only works on the main thread; in tests or
            # embedded use cases (e.g. ``uvicorn.run`` inside a thread)
            # this is a no-op and we fall back to lifespan-based shutdown.
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "Daemon starting on %s:%d (poll=%ss, stale_after=%ss)",
        _settings.host,
        _settings.port,
        _settings.poll_interval_seconds,
        _settings.stale_after_seconds,
    )
    _install_signal_handlers()
    _service.start()
    try:
        yield
    finally:
        log.info("Daemon shutting down")
        _service.stop()


app = FastAPI(
    title="glucose-daemon",
    version="0.2.0",
    description="Personal LibreLinkUp polling daemon — local HTTP API.",
    lifespan=lifespan,
)


# --- helpers ---


def _now() -> datetime:
    return datetime.now(UTC)


def _sensor_utc(reading) -> datetime | None:  # noqa: ANN001
    """Return the reading's UTC timestamp.

    pylibrelinkup exposes two timestamps:

    * ``timestamp`` — naive, in the *user's local time* despite no tzinfo.
      Useless for age math without knowing the user's TZ.
    * ``factory_timestamp`` — tz-aware UTC. This is the canonical one.

    We prefer ``factory_timestamp``; fall back to treating ``timestamp`` as
    UTC only if ``factory_timestamp`` is absent (defensive — should never
    happen with current pylibrelinkup)."""
    ft = getattr(reading, "factory_timestamp", None)
    if ft is not None:
        return ft if ft.tzinfo else ft.replace(tzinfo=UTC)
    ts = getattr(reading, "timestamp", None)
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _build_current(snapshot) -> CurrentResponse:  # noqa: ANN001
    if snapshot.reading is None:
        return CurrentResponse(status=snapshot.status)
    r = snapshot.reading
    sensor_ts = _sensor_utc(r)
    fetched_at = snapshot.fetched_at
    now = _now()
    return CurrentResponse(
        status=snapshot.status,
        unit=glucose_unit_label(getattr(r, "glucose_units", None)),
        value=float(r.value),
        value_in_mg_per_dl=float(getattr(r, "value_in_mg_per_dl", r.value)),
        trend=trend_name(getattr(r, "trend", None)),
        trend_symbol=trend_symbol(getattr(r, "trend", None)),
        sensor_timestamp=sensor_ts,
        fetched_at=fetched_at,
        reading_age_seconds=int((now - sensor_ts).total_seconds()) if sensor_ts else None,
        fetch_age_seconds=int((now - fetched_at).total_seconds()) if fetched_at else None,
        is_high=getattr(r, "is_high", None),
        is_low=getattr(r, "is_low", None),
    )


# --- routes ---


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    snap = _service.snapshot()
    uptime = int((_now() - snap.started_at).total_seconds())
    return HealthResponse(
        status=http_status_summary(snap.status),
        daemon_status=snap.status,
        uptime_seconds=uptime,
        last_error=snap.last_error,
        consecutive_failures=snap.consecutive_failures,
    )


@app.get("/glucose/current", response_model=CurrentResponse)
async def current() -> CurrentResponse:
    snap = _service.snapshot()
    if snap.reading is None and snap.status == Status.STARTING:
        # Genuine "not ready" — different from operational degradations.
        raise HTTPException(status_code=503, detail="Daemon starting; no reading yet")
    return _build_current(snap)


@app.get("/glucose/graph", response_model=list[GraphPoint])
async def graph(hours: int = 12) -> list[GraphPoint]:
    """Return historical readings.

    Note: ``pylibrelinkup.graph()`` returns up to 12 hours; we honor ``hours``
    only as a client-side trim. This call hits Abbott synchronously inside an
    async handler — fine for a personal tool with one client.
    """
    if _service.patient is None:
        raise HTTPException(status_code=503, detail="Daemon not authenticated")
    try:
        readings = _service.client.graph(_service.patient)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}") from e
    cutoff = _now().timestamp() - hours * 3600
    points = []
    for r in readings:
        ts = r.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts.timestamp() >= cutoff:
            points.append(GraphPoint(value=float(r.value), timestamp=ts))
    return points


@app.get("/patient", response_model=PatientResponse)
async def patient() -> PatientResponse:
    p = _service.patient
    if p is None:
        raise HTTPException(status_code=503, detail="Daemon not authenticated")
    return PatientResponse(
        first_name=p.first_name,
        last_name=p.last_name,
        patient_id=str(getattr(p, "patient_id", "") or "") or None,
    )


@app.post("/auth/refresh", response_model=HealthResponse)
async def auth_refresh() -> HealthResponse:
    _service.force_refresh()
    return await health()
