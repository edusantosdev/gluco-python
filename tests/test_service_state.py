"""State-machine tests for ``GlucoseService`` using a fake LLU client.

We don't exercise the polling thread here — driving ``_tick`` synchronously
is far easier to reason about and avoids flaky timing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pylibrelinkup import (
    APIUrl,
    AuthenticationError,
    LLUAPIRateLimitError,
)

from daemon.backoff import ExponentialBackoff, seed_for_tests
from daemon.service import GlucoseService
from daemon.session_store import FileSessionStore
from daemon.state import Status


# --- fakes ---


@dataclass
class FakePatient:
    first_name: str = "T"
    last_name: str = "Test"
    patient_id: str = "00000000-0000-0000-0000-000000000000"


def _fake_reading(age_seconds: float, value: float = 120.0):
    ts = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return SimpleNamespace(
        value=value,
        value_in_mg_per_dl=value,
        factory_timestamp=ts,
        timestamp=ts,
        glucose_units=1,
        trend=None,
        is_high=False,
        is_low=False,
    )


def _fake_graph_response(
    *,
    reading_age_seconds: float = 30,
    history: list | None = None,
    activation_offset_seconds: float = -3600,
    warmup_minutes: int = 60,
):
    """Build a GraphResponse-shaped object.

    ``activation_offset_seconds``: seconds *before now* (negative = past);
    ``warmup_minutes``: simulated sensor warm-up duration."""
    sensor = SimpleNamespace(
        a=int(time.time() + activation_offset_seconds),
        w=warmup_minutes,
    )
    connection = SimpleNamespace(
        sensor=sensor,
        glucose_measurement=_fake_reading(reading_age_seconds),
    )
    data = SimpleNamespace(
        connection=connection,
        graph_data=history if history is not None else [_fake_reading(i * 60) for i in range(3)],
    )
    return SimpleNamespace(data=data)


class FakeClient:
    """Drop-in for ``PyLibreLinkUp`` — records calls, returns scripted results."""

    def __init__(self) -> None:
        self.token = None
        self.account_id_hash = None
        self.authenticate_calls = 0
        self.get_patients_calls = 0
        self.read_calls = 0
        # Test-controlled behaviour:
        self.auth_raises: Exception | None = None
        self.get_patients_result: list = field(default_factory=list)  # type: ignore[assignment]
        self.get_patients_raises: Exception | None = None
        self.read_queue: list = []  # pop(0) each call; Exception → raise
        self.patients_default: list = [FakePatient()]

    def authenticate(self) -> None:
        self.authenticate_calls += 1
        if self.auth_raises is not None:
            raise self.auth_raises
        # Simulate a successful auth setting token + hash.
        self.token = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.sig"
        self.account_id_hash = "deadbeef"

    def get_patients(self):
        self.get_patients_calls += 1
        if self.get_patients_raises is not None:
            raise self.get_patients_raises
        return list(self.patients_default)

    def read(self, patient):
        self.read_calls += 1
        if not self.read_queue:
            raise RuntimeError("read_queue empty — test didn't queue a response")
        item = self.read_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# --- fixtures ---


@pytest.fixture
def service(tmp_path, monkeypatch) -> GlucoseService:
    """GlucoseService with its LLU client replaced by FakeClient."""
    # Stub the real PyLibreLinkUp so `_authenticate_once(force=True)` keeps
    # using the same FakeClient (otherwise it re-instantiates the real one).
    fake = FakeClient()
    monkeypatch.setattr(
        "daemon.service.PyLibreLinkUp", lambda email, password, api_url: fake
    )
    svc = GlucoseService(
        email="x@y",
        password="p",
        region=APIUrl.US,
        poll_interval=60,
        stale_after_seconds=300,
        session_store=FileSessionStore(tmp_path / "session.json"),
        error_backoff=ExponentialBackoff(base_seconds=1, cap_seconds=8, jitter="none"),
        ratelimit_backoff=ExponentialBackoff(base_seconds=1, cap_seconds=8, jitter="none"),
    )
    # The service constructor already instantiated one client; swap it
    # for our fake so captured calls land on the same object the monkeypatch returns.
    svc.client = fake
    seed_for_tests(svc._error_backoff, 0)
    seed_for_tests(svc._ratelimit_backoff, 0)
    return svc


# --- tests ---


def test_authenticate_persists_session(service: GlucoseService) -> None:
    assert service._authenticate_once() is True
    assert service.snapshot().status != Status.AUTH_FAILED
    stored = service._session_store.load()
    assert stored is not None
    assert stored.token == service.client.token


def test_restore_session_skips_login(service: GlucoseService) -> None:
    # First auth primes the session file.
    assert service._authenticate_once()
    before = service.client.authenticate_calls
    # Restore path should not call authenticate() again.
    assert service._try_restore_session() is True
    service._load_patient()
    assert service.client.authenticate_calls == before


def test_tick_records_ok_reading(service: GlucoseService) -> None:
    assert service._authenticate_once()
    service.client.read_queue.append(_fake_graph_response(reading_age_seconds=30))
    sleep_for = service._tick()
    snap = service.snapshot()
    assert snap.status == Status.OK
    assert snap.reading is not None
    assert sleep_for == service.poll_interval  # reset on success


def test_tick_caches_normalized_history(service: GlucoseService) -> None:
    """Poll loop should publish ``(utc_ts, mg_dl)`` tuples into ``state.history``."""
    assert service._authenticate_once()
    history = [_fake_reading(age_seconds=i * 60, value=100.0 + i) for i in range(5)]
    service.client.read_queue.append(
        _fake_graph_response(reading_age_seconds=30, history=history)
    )
    service._tick()
    cached = service.snapshot().history
    assert len(cached) == len(history)
    # Sorted oldest-first.
    assert all(cached[i][0] <= cached[i + 1][0] for i in range(len(cached) - 1))
    # Values preserved as mg/dL floats.
    assert {round(v, 0) for _, v in cached} == {100.0, 101.0, 102.0, 103.0, 104.0}


def test_tick_drops_history_older_than_retention(service: GlucoseService) -> None:
    """Points outside the 12h retention window should be filtered out."""
    assert service._authenticate_once()
    history = [
        _fake_reading(age_seconds=60, value=110.0),  # fresh, kept
        _fake_reading(age_seconds=13 * 3600, value=200.0),  # 13h old, dropped
    ]
    service.client.read_queue.append(
        _fake_graph_response(reading_age_seconds=60, history=history)
    )
    service._tick()
    cached = service.snapshot().history
    # Old point dropped; the other fresh point stays. The current measurement
    # is also appended (newer than the retained sample), so we expect two.
    assert 200 not in {round(v) for _, v in cached}
    assert any(round(v) == 110 for _, v in cached)


def test_tick_appends_current_reading_when_newer_than_history(
    service: GlucoseService,
) -> None:
    """The rightmost graph point must equal the current ``glucose_measurement``.

    LibreView's ``graph_data`` only refreshes every ~15 min while the
    headline reading updates every minute, so the daemon must append the
    current measurement when it's newer to keep the menubar number and
    the graph's last dot in sync.
    """
    assert service._authenticate_once()
    # History samples 5, 10, 15 min old — none match the current reading.
    history = [
        _fake_reading(age_seconds=15 * 60, value=150.0),
        _fake_reading(age_seconds=10 * 60, value=160.0),
        _fake_reading(age_seconds=5 * 60, value=170.0),
    ]
    service.client.read_queue.append(
        _fake_graph_response(reading_age_seconds=30, history=history)
    )
    service._tick()
    cached = service.snapshot().history
    # Sorted oldest-first, so the newest value should be the current reading's.
    assert cached[-1][1] == 120.0  # _fake_reading default value
    assert len(cached) == len(history) + 1


def test_tick_does_not_duplicate_when_current_matches_latest_history(
    service: GlucoseService,
) -> None:
    """If graph_data's newest sample is the current reading, don't duplicate it."""
    assert service._authenticate_once()
    shared = _fake_reading(age_seconds=0, value=123.0)
    history = [
        _fake_reading(age_seconds=5 * 60, value=150.0),
        shared,
    ]
    resp = _fake_graph_response(history=history)
    resp.data.connection.glucose_measurement = shared  # same object ⇒ same ts
    service.client.read_queue.append(resp)
    service._tick()
    cached = service.snapshot().history
    assert len(cached) == 2
    assert cached[-1][1] == 123.0


def test_tick_marks_stale_when_reading_too_old(service: GlucoseService) -> None:
    assert service._authenticate_once()
    service.client.read_queue.append(
        _fake_graph_response(reading_age_seconds=service.stale_after + 60)
    )
    service._tick()
    assert service.snapshot().status == Status.STALE


def test_tick_detects_warmup(service: GlucoseService) -> None:
    """Empty history + recent sensor activation ⇒ WARMING_UP, not STALE."""
    assert service._authenticate_once()
    service.client.read_queue.append(
        _fake_graph_response(
            history=[],
            activation_offset_seconds=-600,  # 10 min ago
            warmup_minutes=60,  # still 50 min left
        )
    )
    service._tick()
    assert service.snapshot().status == Status.WARMING_UP


def test_tick_retries_once_on_auth_error(service: GlucoseService) -> None:
    assert service._authenticate_once()
    # First poll raises 401; we expect _tick to re-auth and retry *once*.
    service.client.read_queue.append(AuthenticationError("expired"))
    service.client.read_queue.append(_fake_graph_response(reading_age_seconds=30))
    service._tick()
    assert service.snapshot().status == Status.OK
    # authenticate() called twice: initial + one refresh.
    assert service.client.authenticate_calls == 2


def test_tick_uses_backoff_on_network_error(service: GlucoseService) -> None:
    assert service._authenticate_once()
    service.client.read_queue.append(ConnectionError("boom"))
    service.client.read_queue.append(ConnectionError("boom"))
    service.client.read_queue.append(ConnectionError("boom"))

    delays = [service._tick(), service._tick(), service._tick()]
    # Base=1, factor=2 → 1, 2, 4 (no jitter).
    assert delays == [1, 2, 4]
    assert service.snapshot().status == Status.NO_NETWORK
    assert service.snapshot().consecutive_failures == 3


def test_tick_resets_backoff_after_success(service: GlucoseService) -> None:
    assert service._authenticate_once()
    service.client.read_queue.extend(
        [
            ConnectionError("x"),
            ConnectionError("x"),
            _fake_graph_response(reading_age_seconds=30),
            ConnectionError("x"),
        ]
    )
    service._tick()  # 1s
    service._tick()  # 2s
    service._tick()  # success → reset
    delay = service._tick()  # should be base again
    assert delay == 1


def test_ratelimit_honors_retry_after(service: GlucoseService) -> None:
    assert service._authenticate_once()
    err = LLUAPIRateLimitError(response_code=429, message="slow down", retry_after=42)
    service.client.read_queue.append(err)
    delay = service._tick()
    assert delay == 42
    assert service.snapshot().status == Status.RATE_LIMITED


def test_ratelimit_falls_back_to_backoff_without_retry_after(
    service: GlucoseService,
) -> None:
    assert service._authenticate_once()
    err = LLUAPIRateLimitError(response_code=429, message="slow down", retry_after=None)
    service.client.read_queue.extend([err, err])
    d1 = service._tick()
    d2 = service._tick()
    # ratelimit backoff is base=1, factor=2 → 1, 2
    assert d1 == 1
    assert d2 == 2


def test_auth_failed_clears_session(service: GlucoseService) -> None:
    assert service._authenticate_once()
    assert service._session_store.load() is not None
    service.client.auth_raises = AuthenticationError("bad creds")
    service._authenticate_once(force=True)
    assert service.snapshot().status == Status.AUTH_FAILED
    assert service._session_store.load() is None
