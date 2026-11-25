"""Tests for the glucose alert rules and notification prefs."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from macos_frontend.notifications import (
    DEFAULT_THRESHOLDS,
    HIGH,
    IN_RANGE,
    LOW,
    VERY_HIGH,
    AlertEngine,
    AlertThresholds,
    NotificationPrefs,
    Observation,
    effective_thresholds,
    format_threshold,
    load_prefs,
    next_zone,
    parse_threshold_input,
    save_prefs,
    validate_thresholds,
)

T = DEFAULT_THRESHOLDS
ON = NotificationPrefs(enabled=True, remind_minutes=0, rapid=False, signal_loss=False)


class FakeClock:
    def __init__(self) -> None:
        self.wall = 1_800_000_000.0
        self.mono = 1000.0

    def advance(self, seconds: float, *, asleep: bool = False) -> None:
        self.wall += seconds
        if not asleep:
            self.mono += seconds

    def now_dt(self) -> datetime:
        return datetime.fromtimestamp(self.wall, UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def engine(clock: FakeClock) -> AlertEngine:
    return AlertEngine(clock=lambda: clock.wall, monotonic=lambda: clock.mono)


def reading(clock: FakeClock, mg: float, **kw) -> Observation:
    base = dict(
        status="ok",
        value_mg_dl=mg,
        value=mg,
        unit="mg/dL",
        sensor_dt=clock.now_dt(),
    )
    base.update(kw)
    return Observation(**base)


def kinds(alerts) -> list[str]:
    return [a.kind for a in alerts]


# --- zones -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "value", "expected"),
    [
        (IN_RANGE, 70, IN_RANGE),  # boundary is in range, like the colours
        (IN_RANGE, 69, LOW),
        (IN_RANGE, 180, IN_RANGE),
        (IN_RANGE, 181, HIGH),
        (IN_RANGE, 251, VERY_HIGH),
        (LOW, 72, LOW),  # within hysteresis
        (LOW, 75, IN_RANGE),
        (LOW, 200, HIGH),
        (HIGH, 178, HIGH),
        (HIGH, 175, IN_RANGE),
        (HIGH, 260, VERY_HIGH),
        (HIGH, 60, LOW),
        (VERY_HIGH, 248, VERY_HIGH),
        (VERY_HIGH, 240, HIGH),
        (VERY_HIGH, 170, IN_RANGE),
    ],
)
def test_next_zone(current, value, expected):
    assert next_zone(current, value, T) == expected


# --- range alerts --------------------------------------------------------------


def test_low_fires_once_then_rearms_after_recovery(engine, clock):
    assert engine.evaluate(ON, reading(clock, 100)) == []
    alerts = engine.evaluate(ON, reading(clock, 65))
    assert kinds(alerts) == [LOW]
    assert alerts[0].title == "Low glucose"
    assert alerts[0].body == "65 mg/dL"
    assert alerts[0].time_sensitive is True
    clock.advance(300)
    assert engine.evaluate(ON, reading(clock, 60)) == []
    # Hovering just above the line doesn't count as recovered.
    assert engine.evaluate(ON, reading(clock, 72)) == []
    assert engine.evaluate(ON, reading(clock, 68)) == []
    assert engine.evaluate(ON, reading(clock, 90)) == []
    assert kinds(engine.evaluate(ON, reading(clock, 65))) == [LOW]


def test_reminder_repeats_while_out_of_range(engine, clock):
    prefs = replace(ON, remind_minutes=15)
    assert kinds(engine.evaluate(prefs, reading(clock, 200))) == [HIGH]
    clock.advance(14 * 60)
    assert engine.evaluate(prefs, reading(clock, 205)) == []
    clock.advance(60)
    alerts = engine.evaluate(prefs, reading(clock, 210, trend_symbol="↗"))
    assert kinds(alerts) == [HIGH]
    assert alerts[0].title == "Still high"
    assert alerts[0].body == "210 mg/dL ↗"


def test_only_once_never_reminds(engine, clock):
    assert kinds(engine.evaluate(ON, reading(clock, 200))) == [HIGH]
    clock.advance(6 * 3600)
    assert engine.evaluate(ON, reading(clock, 200)) == []


def test_very_high_escalates_but_does_not_realert_on_the_way_down(engine, clock):
    prefs = replace(ON, very_high=True)
    assert kinds(engine.evaluate(prefs, reading(clock, 200))) == [HIGH]
    assert kinds(engine.evaluate(prefs, reading(clock, 260))) == [VERY_HIGH]
    assert engine.evaluate(prefs, reading(clock, 230)) == []
    assert engine.evaluate(prefs, reading(clock, 200)) == []


def test_very_high_off_treats_it_as_high(engine, clock):
    prefs = replace(ON, very_high=False)
    assert kinds(engine.evaluate(prefs, reading(clock, 300))) == [HIGH]
    assert engine.evaluate(prefs, reading(clock, 200)) == []


def test_only_very_high_enabled(engine, clock):
    prefs = replace(ON, high=False, very_high=True)
    assert engine.evaluate(prefs, reading(clock, 200)) == []
    assert kinds(engine.evaluate(prefs, reading(clock, 260))) == [VERY_HIGH]


def test_disabled_alert_type_is_silent(engine, clock):
    assert engine.evaluate(replace(ON, low=False), reading(clock, 50)) == []


def test_reenabling_alert_type_while_out_of_range_notifies(engine, clock):
    engine.evaluate(replace(ON, high=False), reading(clock, 200))
    assert kinds(engine.evaluate(ON, reading(clock, 200))) == [HIGH]


def test_master_switch_off_resets_state(engine, clock):
    assert kinds(engine.evaluate(ON, reading(clock, 200))) == [HIGH]
    assert engine.evaluate(replace(ON, enabled=False), reading(clock, 200)) == []
    assert kinds(engine.evaluate(ON, reading(clock, 200))) == [HIGH]


def test_stale_reading_does_not_trigger_range(engine, clock):
    assert engine.evaluate(ON, reading(clock, 50, status="stale")) == []


def test_brief_outage_keeps_range_state(engine, clock):
    assert kinds(engine.evaluate(ON, reading(clock, 50))) == [LOW]
    engine.evaluate(ON, Observation(status="no_network", sensor_dt=clock.now_dt()))
    assert engine.evaluate(ON, reading(clock, 52)) == []


def test_custom_thresholds(engine, clock):
    prefs = replace(
        ON,
        use_custom_thresholds=True,
        custom_low_mg_dl=80,
        custom_high_mg_dl=160,
        custom_very_high_mg_dl=220,
    )
    assert effective_thresholds(prefs) == AlertThresholds(80, 160, 220)
    assert kinds(engine.evaluate(prefs, reading(clock, 75))) == [LOW]


def test_mmol_body_uses_display_unit(engine, clock):
    obs = reading(clock, 63, value=3.5, unit="mmol/L", trend_symbol="↓")
    alerts = engine.evaluate(ON, obs)
    assert alerts[0].body == "3.5 mmol/L ↓"


# --- rapid change --------------------------------------------------------------


def test_rapid_change_once_per_episode_with_cooldown(engine, clock):
    prefs = replace(ON, rapid=True)
    fast = dict(trend="falling_fast", trend_symbol="↓")
    alerts = engine.evaluate(prefs, reading(clock, 120, **fast))
    assert kinds(alerts) == ["falling_fast"]
    assert alerts[0].title == "Glucose falling fast"
    clock.advance(60)
    assert engine.evaluate(prefs, reading(clock, 115, **fast)) == []
    # Arrow flickers back and forth within the cooldown: no repeat.
    engine.evaluate(prefs, reading(clock, 112, trend="falling", trend_symbol="↘"))
    assert engine.evaluate(prefs, reading(clock, 108, **fast)) == []
    clock.advance(16 * 60)
    engine.evaluate(prefs, reading(clock, 100, trend="stable", trend_symbol="→"))
    assert kinds(engine.evaluate(prefs, reading(clock, 95, **fast))) == ["falling_fast"]


def test_rapid_change_other_direction_not_blocked(engine, clock):
    prefs = replace(ON, rapid=True)
    engine.evaluate(prefs, reading(clock, 120, trend="falling_fast"))
    assert kinds(engine.evaluate(prefs, reading(clock, 125, trend="rising_fast"))) == [
        "rising_fast"
    ]


def test_slow_arrows_do_not_alert(engine, clock):
    prefs = replace(ON, rapid=True)
    assert engine.evaluate(prefs, reading(clock, 120, trend="falling")) == []
    assert engine.evaluate(prefs, reading(clock, 120, trend="rising")) == []


def test_rapid_falls_back_to_symbol(engine, clock):
    prefs = replace(ON, rapid=True)
    assert kinds(engine.evaluate(prefs, reading(clock, 150, trend_symbol="↑"))) == [
        "rising_fast"
    ]


# --- signal loss ---------------------------------------------------------------


def test_signal_loss_fires_once_and_rearms(engine, clock):
    prefs = replace(ON, signal_loss=True, signal_loss_minutes=20)
    last = clock.now_dt()
    assert engine.evaluate(prefs, reading(clock, 100, sensor_dt=last)) == []
    clock.advance(19 * 60)
    assert engine.evaluate(prefs, Observation(status="stale", sensor_dt=last)) == []
    clock.advance(60)
    alerts = engine.evaluate(prefs, Observation(status="no_network", sensor_dt=last))
    assert kinds(alerts) == ["signal_loss"]
    assert "20 minutes" in alerts[0].body
    clock.advance(30 * 60)
    assert engine.evaluate(prefs, Observation(status="stale", sensor_dt=last)) == []
    # A new reading re-arms it.
    assert engine.evaluate(prefs, reading(clock, 100)) == []
    last = clock.now_dt()
    clock.advance(20 * 60)
    assert kinds(engine.evaluate(prefs, Observation(status="stale", sensor_dt=last))) == [
        "signal_loss"
    ]


def test_signal_loss_waits_after_sleep(engine, clock):
    prefs = replace(ON, signal_loss=True, signal_loss_minutes=20)
    last = clock.now_dt()
    engine.evaluate(prefs, reading(clock, 100, sensor_dt=last))
    clock.advance(8 * 3600, asleep=True)
    # Just woke up: the reading is hours old but the daemon hasn't had a
    # chance to fetch yet.
    assert engine.evaluate(prefs, Observation(status="stale", sensor_dt=last)) == []
    clock.advance(20 * 60)
    assert kinds(engine.evaluate(prefs, Observation(status="stale", sensor_dt=last))) == [
        "signal_loss"
    ]


def test_signal_loss_off(engine, clock):
    prefs = replace(ON, signal_loss=False)
    last = clock.now_dt()
    engine.evaluate(prefs, reading(clock, 100, sensor_dt=last))
    clock.advance(3600)
    assert engine.evaluate(prefs, Observation(status="stale", sensor_dt=last)) == []


def test_logged_out_is_silent(engine, clock):
    prefs = replace(ON, signal_loss=True)
    last = clock.now_dt()
    engine.evaluate(prefs, reading(clock, 100, sensor_dt=last))
    clock.advance(3600)
    assert engine.evaluate(prefs, Observation(status="logged_out", sensor_dt=None)) == []


# --- units / parsing -----------------------------------------------------------


def test_threshold_formatting_and_parsing():
    assert format_threshold(70, "mg/dL") == "70"
    assert format_threshold(70, "mmol/L") == "3.9"
    assert format_threshold(180, None) == "180"
    assert parse_threshold_input("3,9", "mmol/L") == pytest.approx(70.2)
    assert parse_threshold_input(" 80 ", "mg/dL") == 80
    assert parse_threshold_input("abc", "mg/dL") is None
    assert parse_threshold_input("", "mg/dL") is None
    assert parse_threshold_input("nan", "mg/dL") is None


def test_validate_thresholds():
    assert validate_thresholds(70, 180, 250) is None
    assert validate_thresholds(7, 180, 250) == "Low must be between 40 and 150 mg/dL."
    assert validate_thresholds(120, 110, 250) == "Low must be below High."
    assert validate_thresholds(70, 250, 250) == "High must be below Very high."
    assert (
        validate_thresholds(7, 180, 250, unit="mmol/L")
        == "Low must be between 2.2 and 8.3 mmol/L."
    )


# --- prefs file ----------------------------------------------------------------


def test_prefs_round_trip(tmp_path):
    path = tmp_path / "notifications.json"
    prefs = replace(
        NotificationPrefs(),
        enabled=True,
        remind_minutes=30,
        use_custom_thresholds=True,
        custom_low_mg_dl=80.0,
    )
    save_prefs(path, prefs)
    assert load_prefs(path) == prefs


def test_missing_or_corrupt_prefs_fall_back_to_defaults(tmp_path):
    assert load_prefs(tmp_path / "absent.json") == NotificationPrefs()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_prefs(bad) == NotificationPrefs()
    bad.write_text("[1, 2]")
    assert load_prefs(bad) == NotificationPrefs()


def test_prefs_sanitize_bad_values(tmp_path):
    path = tmp_path / "notifications.json"
    path.write_text(
        json.dumps(
            {
                "enabled": "yes",
                "remind_minutes": 7,
                "signal_loss_minutes": 45,
                "sound": False,
                "use_custom_thresholds": True,
                "custom_low_mg_dl": 200,
                "custom_high_mg_dl": 180,
            }
        )
    )
    prefs = load_prefs(path)
    assert prefs.enabled is False
    assert prefs.remind_minutes == NotificationPrefs.remind_minutes
    assert prefs.signal_loss_minutes == NotificationPrefs.signal_loss_minutes
    assert prefs.sound is False
    assert prefs.use_custom_thresholds is False
    assert effective_thresholds(prefs) == DEFAULT_THRESHOLDS
