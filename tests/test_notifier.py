"""Notification policy: hysteresis, cooldown, and staleness detection."""

from __future__ import annotations

from macos_frontend.notifier import (
    NotificationEvent,
    NotificationPolicy,
    Notifier,
    Zone,
)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _policy(clock: FakeClock, **overrides) -> NotificationPolicy:
    defaults = dict(
        low=70,
        high=180,
        very_high=250,
        hysteresis_mg_dl=5,
        cooldown_seconds=15 * 60,
        stale_after_seconds=10 * 60,
        stale_cooldown_seconds=30 * 60,
    )
    defaults.update(overrides)
    p = NotificationPolicy(**defaults)
    p._now = clock
    return p


# --- threshold transitions ------------------------------------------------


def test_first_ok_reading_does_not_fire() -> None:
    clock = FakeClock()
    p = _policy(clock)
    events = p.evaluate(value_mg_dl=120, reading_age_seconds=30, status="ok")
    assert events == []


def test_transition_to_low_fires() -> None:
    clock = FakeClock()
    p = _policy(clock)
    p.evaluate(value_mg_dl=120, reading_age_seconds=30, status="ok")
    events = p.evaluate(value_mg_dl=65, reading_age_seconds=30, status="ok")
    assert len(events) == 1
    assert events[0].kind == "threshold"
    assert "Low glucose" in events[0].title


def test_transition_to_high_fires_then_suppresses() -> None:
    clock = FakeClock()
    p = _policy(clock)
    p.evaluate(value_mg_dl=120, reading_age_seconds=30, status="ok")
    e1 = p.evaluate(value_mg_dl=195, reading_age_seconds=30, status="ok")
    assert len(e1) == 1 and "High glucose" in e1[0].title
    # Still HIGH 5 min later — shouldn't re-fire.
    clock.advance(5 * 60)
    e2 = p.evaluate(value_mg_dl=200, reading_age_seconds=30, status="ok")
    assert e2 == []


def test_cooldown_allows_refire_after_window_lapses() -> None:
    clock = FakeClock()
    p = _policy(clock, cooldown_seconds=10 * 60)
    p.evaluate(value_mg_dl=120, reading_age_seconds=30, status="ok")
    assert p.evaluate(value_mg_dl=195, reading_age_seconds=30, status="ok")
    # Drop back in range, then go high again past the cooldown.
    p.evaluate(value_mg_dl=130, reading_age_seconds=30, status="ok")
    clock.advance(11 * 60)
    e = p.evaluate(value_mg_dl=200, reading_age_seconds=30, status="ok")
    assert len(e) == 1


def test_hysteresis_prevents_flapping_around_threshold() -> None:
    """178 ↔ 182 straddles ``high``; we want one alert, not four."""
    clock = FakeClock()
    p = _policy(clock)
    p.evaluate(value_mg_dl=150, reading_age_seconds=30, status="ok")  # prime IN_RANGE
    assert p.evaluate(value_mg_dl=182, reading_age_seconds=30, status="ok")
    assert p.evaluate(value_mg_dl=178, reading_age_seconds=30, status="ok") == []
    assert p.evaluate(value_mg_dl=183, reading_age_seconds=30, status="ok") == []
    assert p.evaluate(value_mg_dl=179, reading_age_seconds=30, status="ok") == []


def test_escalation_high_to_very_high_fires() -> None:
    clock = FakeClock()
    p = _policy(clock)
    p.evaluate(value_mg_dl=150, reading_age_seconds=30, status="ok")
    assert p.evaluate(value_mg_dl=195, reading_age_seconds=30, status="ok")
    events = p.evaluate(value_mg_dl=270, reading_age_seconds=30, status="ok")
    assert len(events) == 1
    assert "Very high" in events[0].title


def test_low_fires_immediately_even_from_fresh_state() -> None:
    """Hypo must not be delayed by hysteresis — first reading below low wins."""
    clock = FakeClock()
    p = _policy(clock)
    events = p.evaluate(value_mg_dl=55, reading_age_seconds=30, status="ok")
    assert len(events) == 1 and "Low glucose" in events[0].title


def test_non_ok_status_suppresses_threshold_alerts() -> None:
    """Stale/no-network shouldn't lob hypo alerts based on old numbers."""
    clock = FakeClock()
    p = _policy(clock)
    events = p.evaluate(value_mg_dl=55, reading_age_seconds=3600, status="stale")
    # Stale alert still fires because the age threshold is crossed, but no threshold one.
    assert all(ev.kind != "threshold" for ev in events)


# --- staleness ------------------------------------------------------------


def test_stale_alert_fires_once_per_cooldown() -> None:
    clock = FakeClock()
    p = _policy(clock, stale_after_seconds=600, stale_cooldown_seconds=1800)
    e1 = p.evaluate(value_mg_dl=120, reading_age_seconds=700, status="stale")
    assert len(e1) == 1 and e1[0].kind == "stale"
    clock.advance(10 * 60)
    # Still stale, but inside cooldown.
    assert p.evaluate(value_mg_dl=120, reading_age_seconds=1200, status="stale") == []
    clock.advance(21 * 60)
    e2 = p.evaluate(value_mg_dl=120, reading_age_seconds=2500, status="stale")
    assert len(e2) == 1 and e2[0].kind == "stale"


def test_stale_not_fired_when_reading_fresh() -> None:
    clock = FakeClock()
    p = _policy(clock, stale_after_seconds=600)
    assert p.evaluate(value_mg_dl=120, reading_age_seconds=60, status="ok") == []


# --- notifier wrapper -----------------------------------------------------


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def notify(self, *, title: str, subtitle, message: str) -> None:
        self.calls.append({"title": title, "subtitle": subtitle, "message": message})


def test_notifier_delivers_only_when_enabled() -> None:
    clock = FakeClock()
    policy = _policy(clock)
    backend = RecordingBackend()
    n = Notifier(policy, backend=backend, enabled=True)
    policy.evaluate(value_mg_dl=120, reading_age_seconds=30, status="ok")  # prime
    events = n.handle(value_mg_dl=55, reading_age_seconds=30, status="ok")
    assert len(events) == 1 and len(backend.calls) == 1

    n.enabled = False
    # advance past cooldown to allow another fire
    clock.advance(20 * 60)
    policy.evaluate(value_mg_dl=120, reading_age_seconds=30, status="ok")
    events = n.handle(value_mg_dl=55, reading_age_seconds=30, status="ok")
    assert len(events) == 1  # policy still decided to fire
    assert len(backend.calls) == 1  # but backend stays silent


def test_notifier_returns_events_shape() -> None:
    clock = FakeClock()
    policy = _policy(clock)
    n = Notifier(policy, backend=RecordingBackend(), enabled=True)
    policy.evaluate(value_mg_dl=120, reading_age_seconds=30, status="ok")
    events = n.handle(value_mg_dl=55, reading_age_seconds=30, status="ok")
    assert isinstance(events, list)
    assert all(isinstance(e, NotificationEvent) for e in events)


def test_zone_enum_roundtrip() -> None:
    """Zone is used as a dict key; guard against accidentally breaking that."""
    d = {Zone.LOW: 1, Zone.HIGH: 2}
    assert d[Zone.LOW] == 1 and d[Zone.HIGH] == 2
