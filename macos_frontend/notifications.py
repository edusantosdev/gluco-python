"""Glucose alert rules and notification preferences (pure Python, unit-testable).

The menubar app feeds every poll into :class:`AlertEngine`, which decides
whether a macOS notification should go out. Delivery itself lives in
``macos_frontend.notifier`` so this module never touches AppKit.

Alert types:

* **Range** — low / high / very high. Fires when the reading enters an
  out-of-range zone, optionally re-reminds every N minutes while it stays
  there, and re-arms once the reading is back in range. A small
  hysteresis band around each threshold keeps a reading that hovers on
  the line from flapping in and out of the zone.
* **Rapid change** — LibreView's ``rising_fast`` / ``falling_fast`` trend.
  Once per episode, with a cooldown so a flickering arrow doesn't spam.
* **Signal loss** — no new sensor reading for the configured number of
  minutes (covers sensor gaps, phone/network outages and sign-in
  failures alike). Fires once and re-arms when a new reading arrives.

All thresholds are stored in mg/dL; the UI converts to the account's
display unit.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("glucose.notifications")

MG_DL_PER_MMOL_L = 18.0

# Same values the menubar colours use.
DEFAULT_LOW_MG_DL = 70.0
DEFAULT_HIGH_MG_DL = 180.0
DEFAULT_VERY_HIGH_MG_DL = 250.0

# Accepted ranges for custom thresholds (mg/dL). Wide enough for any
# reasonable clinical target, tight enough to catch typos like 7 vs 70.
LOW_RANGE_MG_DL = (40.0, 150.0)
HIGH_RANGE_MG_DL = (100.0, 400.0)
VERY_HIGH_RANGE_MG_DL = (120.0, 450.0)

REMIND_CHOICES_MIN = (0, 10, 15, 30, 60)  # 0 = only once
SIGNAL_LOSS_CHOICES_MIN = (10, 20, 30, 60)

# A zone is only left once the reading is this far back across the line.
HYSTERESIS_MG_DL = 5.0
# Minimum gap between two rapid-change alerts in the same direction.
RAPID_COOLDOWN_S = 15 * 60

FAST_TRENDS = {"rising_fast": "rising", "falling_fast": "falling"}
FAST_SYMBOLS = {"↑": "rising", "↓": "falling"}


# --- preferences -------------------------------------------------------------


@dataclass(frozen=True)
class NotificationPrefs:
    enabled: bool = False
    low: bool = True
    high: bool = True
    very_high: bool = False
    rapid: bool = True
    signal_loss: bool = True
    signal_loss_minutes: int = 20
    remind_minutes: int = 15
    sound: bool = True
    use_custom_thresholds: bool = False
    custom_low_mg_dl: float = DEFAULT_LOW_MG_DL
    custom_high_mg_dl: float = DEFAULT_HIGH_MG_DL
    custom_very_high_mg_dl: float = DEFAULT_VERY_HIGH_MG_DL


@dataclass(frozen=True)
class AlertThresholds:
    low: float
    high: float
    very_high: float


DEFAULT_THRESHOLDS = AlertThresholds(
    low=DEFAULT_LOW_MG_DL, high=DEFAULT_HIGH_MG_DL, very_high=DEFAULT_VERY_HIGH_MG_DL
)


def effective_thresholds(prefs: NotificationPrefs) -> AlertThresholds:
    if not prefs.use_custom_thresholds:
        return DEFAULT_THRESHOLDS
    return AlertThresholds(
        low=prefs.custom_low_mg_dl,
        high=prefs.custom_high_mg_dl,
        very_high=prefs.custom_very_high_mg_dl,
    )


def validate_thresholds(
    low: float, high: float, very_high: float, *, unit: str | None = None
) -> str | None:
    """Return a user-facing error message, or ``None`` when the values are usable.

    Values are mg/dL; ``unit`` only picks the unit the message is written in.
    """
    for name, value, (lo, hi) in (
        ("Low", low, LOW_RANGE_MG_DL),
        ("High", high, HIGH_RANGE_MG_DL),
        ("Very high", very_high, VERY_HIGH_RANGE_MG_DL),
    ):
        if not lo <= value <= hi:
            return (
                f"{name} must be between {format_threshold(lo, unit)} and "
                f"{format_threshold(hi, unit)} {unit_label(unit)}."
            )
    if not low < high:
        return "Low must be below High."
    if not high < very_high:
        return "High must be below Very high."
    return None


def _coerce(prefs: NotificationPrefs) -> NotificationPrefs:
    """Snap out-of-menu choices and broken thresholds back to safe values."""
    if prefs.remind_minutes not in REMIND_CHOICES_MIN:
        prefs = replace(prefs, remind_minutes=NotificationPrefs.remind_minutes)
    if prefs.signal_loss_minutes not in SIGNAL_LOSS_CHOICES_MIN:
        prefs = replace(prefs, signal_loss_minutes=NotificationPrefs.signal_loss_minutes)
    if validate_thresholds(
        prefs.custom_low_mg_dl, prefs.custom_high_mg_dl, prefs.custom_very_high_mg_dl
    ):
        prefs = replace(
            prefs,
            use_custom_thresholds=False,
            custom_low_mg_dl=DEFAULT_LOW_MG_DL,
            custom_high_mg_dl=DEFAULT_HIGH_MG_DL,
            custom_very_high_mg_dl=DEFAULT_VERY_HIGH_MG_DL,
        )
    return prefs


def load_prefs(path: Path) -> NotificationPrefs:
    """Read prefs from ``path``; anything missing or malformed falls back to defaults."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NotificationPrefs()
    except (OSError, ValueError):
        log.warning("Ignoring unreadable notification prefs at %s", path)
        return NotificationPrefs()
    if not isinstance(raw, dict):
        return NotificationPrefs()

    defaults = NotificationPrefs()
    kwargs = {}
    for f in fields(NotificationPrefs):
        default = getattr(defaults, f.name)
        value = raw.get(f.name, default)
        if isinstance(default, bool):
            kwargs[f.name] = value if isinstance(value, bool) else default
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
            kwargs[f.name] = value if ok else default
        else:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            kwargs[f.name] = float(value) if ok else default
    return _coerce(NotificationPrefs(**kwargs))


def save_prefs(path: Path, prefs: NotificationPrefs) -> None:
    """Atomically write prefs as JSON (write temp file, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".notifications.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(prefs), fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- units -------------------------------------------------------------------


def is_mmol(unit: str | None) -> bool:
    return bool(unit) and unit.strip().lower().startswith("mmol")


def unit_label(unit: str | None) -> str:
    return "mmol/L" if is_mmol(unit) else "mg/dL"


def mg_dl_to_display(value_mg_dl: float, unit: str | None) -> float:
    return value_mg_dl / MG_DL_PER_MMOL_L if is_mmol(unit) else value_mg_dl


def display_to_mg_dl(value: float, unit: str | None) -> float:
    return value * MG_DL_PER_MMOL_L if is_mmol(unit) else value


def format_display_value(value: float, unit: str | None) -> str:
    """``123`` for mg/dL, ``6.8`` for mmol/L."""
    if is_mmol(unit):
        return f"{value:.1f}"
    return str(int(round(value)))


def format_threshold(value_mg_dl: float, unit: str | None) -> str:
    return format_display_value(mg_dl_to_display(value_mg_dl, unit), unit)


def parse_threshold_input(text: str, unit: str | None) -> float | None:
    """Parse a user-typed threshold (accepts ``,`` decimals) into mg/dL."""
    cleaned = text.strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return display_to_mg_dl(value, unit)


# --- alert engine ------------------------------------------------------------

IN_RANGE = "in_range"
LOW = "low"
HIGH = "high"
VERY_HIGH = "very_high"

_SEVERITY = {HIGH: 1, VERY_HIGH: 2}

CATEGORY_RANGE = "range"
CATEGORY_RAPID = "rapid"
CATEGORY_SIGNAL = "signal"


@dataclass(frozen=True)
class Alert:
    category: str  # CATEGORY_* — one delivered notification slot per category
    kind: str
    title: str
    body: str
    time_sensitive: bool = False


@dataclass(frozen=True)
class Observation:
    """What the menubar knows after one poll."""

    status: str  # daemon status, or a frontend-side one ("no_network", "logged_out", …)
    value_mg_dl: float | None = None
    value: float | None = None  # in the account's display unit
    unit: str | None = None
    trend: str | None = None  # falling_fast | falling | stable | rising | rising_fast
    trend_symbol: str | None = None
    sensor_dt: datetime | None = None  # timestamp of the latest known reading (UTC)


def classify(value_mg_dl: float, t: AlertThresholds) -> str:
    # Same boundary semantics as the menubar colours: 70 is in range,
    # 180 is in range, 180.1 is high, 250.1 is very high.
    if value_mg_dl < t.low:
        return LOW
    if value_mg_dl > t.very_high:
        return VERY_HIGH
    if value_mg_dl > t.high:
        return HIGH
    return IN_RANGE


def next_zone(
    current: str, value_mg_dl: float, t: AlertThresholds, h: float = HYSTERESIS_MG_DL
) -> str:
    """Zone after a new reading, leaving a zone only once ``h`` past the line."""
    raw = classify(value_mg_dl, t)
    if current == IN_RANGE:
        return raw
    if current == LOW:
        return LOW if value_mg_dl < t.low + h else raw
    # current is HIGH or VERY_HIGH
    if raw in (LOW, VERY_HIGH):
        return raw
    if current == VERY_HIGH and value_mg_dl > t.very_high - h:
        return VERY_HIGH
    if value_mg_dl > t.high - h:
        return HIGH
    return IN_RANGE


def _alert_kind_for_zone(zone: str, prefs: NotificationPrefs) -> str | None:
    """Which enabled alert covers ``zone`` (very high falls back to high)."""
    if zone == LOW:
        return LOW if prefs.low else None
    if zone == VERY_HIGH:
        if prefs.very_high:
            return VERY_HIGH
        return HIGH if prefs.high else None
    if zone == HIGH:
        return HIGH if prefs.high else None
    return None


def _is_escalation(old: str | None, new: str) -> bool:
    if old is None:
        return True
    if (old == LOW) != (new == LOW):
        return True  # crossed from one side to the other
    return _SEVERITY.get(new, 0) > _SEVERITY.get(old, 0)


def _fast_direction(trend: str | None, symbol: str | None) -> str | None:
    if trend in FAST_TRENDS:
        return FAST_TRENDS[trend]
    if trend is None and symbol in FAST_SYMBOLS:
        return FAST_SYMBOLS[symbol]
    return None


def _value_text(obs: Observation) -> str:
    if obs.value is not None:
        text = f"{format_display_value(obs.value, obs.unit)} {unit_label(obs.unit)}"
    elif obs.value_mg_dl is not None:
        text = f"{format_display_value(obs.value_mg_dl, None)} mg/dL"
    else:
        return ""
    if obs.trend_symbol:
        text += f" {obs.trend_symbol}"
    return text


_RANGE_TITLES = {LOW: "Low glucose", HIGH: "High glucose", VERY_HIGH: "Very high glucose"}
_REMINDER_TITLES = {LOW: "Still low", HIGH: "Still high", VERY_HIGH: "Still very high"}


class AlertEngine:
    """Turns successive :class:`Observation` s into :class:`Alert` s.

    ``clock`` is wall time (reminders, reading age); ``monotonic`` only
    advances while the Mac is awake, so signal-loss can tell "no data for
    20 minutes" apart from "the lid was closed for 20 minutes".
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._monotonic = monotonic
        self.reset()

    def reset(self) -> None:
        self._zone = IN_RANGE
        self._range_kind: str | None = None
        self._range_notified_at: float | None = None
        self._rapid_direction: str | None = None
        self._rapid_notified: dict[str, float] = {}
        self._signal_sensor_dt: datetime | None = None
        self._signal_progress_mono: float | None = None
        self._signal_fired = False

    def evaluate(self, prefs: NotificationPrefs, obs: Observation) -> list[Alert]:
        if not prefs.enabled or obs.status == "logged_out":
            self.reset()
            return []
        now = self._clock()
        alerts: list[Alert] = []
        # Range and rapid-change only act on a fresh reading; while the
        # data is stale or missing, the signal-loss rule speaks instead
        # and the range state is kept so recovery doesn't re-alert.
        if obs.status == "ok" and obs.value_mg_dl is not None:
            alerts.extend(self._evaluate_range(prefs, obs, now))
            alerts.extend(self._evaluate_rapid(prefs, obs, now))
        alerts.extend(self._evaluate_signal(prefs, obs, now))
        return alerts

    # range ---------------------------------------------------------------

    def _evaluate_range(
        self, prefs: NotificationPrefs, obs: Observation, now: float
    ) -> list[Alert]:
        assert obs.value_mg_dl is not None
        self._zone = next_zone(self._zone, obs.value_mg_dl, effective_thresholds(prefs))
        kind = _alert_kind_for_zone(self._zone, prefs)
        old = self._range_kind
        self._range_kind = kind
        if kind is None:
            self._range_notified_at = None
            return []
        if kind != old and _is_escalation(old, kind):
            self._range_notified_at = now
            return [self._range_alert(kind, obs, reminder=False)]
        remind_s = prefs.remind_minutes * 60
        if (
            remind_s > 0
            and self._range_notified_at is not None
            and now - self._range_notified_at >= remind_s
        ):
            self._range_notified_at = now
            return [self._range_alert(kind, obs, reminder=True)]
        return []

    def _range_alert(self, kind: str, obs: Observation, *, reminder: bool) -> Alert:
        title = (_REMINDER_TITLES if reminder else _RANGE_TITLES)[kind]
        return Alert(
            category=CATEGORY_RANGE,
            kind=kind,
            title=title,
            body=_value_text(obs),
            time_sensitive=kind == LOW,
        )

    # rapid change ---------------------------------------------------------

    def _evaluate_rapid(
        self, prefs: NotificationPrefs, obs: Observation, now: float
    ) -> list[Alert]:
        direction = _fast_direction(obs.trend, obs.trend_symbol)
        previous = self._rapid_direction
        self._rapid_direction = direction
        if not prefs.rapid or direction is None or direction == previous:
            return []
        last = self._rapid_notified.get(direction)
        if last is not None and now - last < RAPID_COOLDOWN_S:
            return []
        self._rapid_notified[direction] = now
        return [
            Alert(
                category=CATEGORY_RAPID,
                kind=f"{direction}_fast",
                title=f"Glucose {direction} fast",
                body=_value_text(obs),
            )
        ]

    # signal loss ----------------------------------------------------------

    def _evaluate_signal(
        self, prefs: NotificationPrefs, obs: Observation, now: float
    ) -> list[Alert]:
        mono = self._monotonic()
        if obs.sensor_dt is None:
            return []
        if obs.sensor_dt != self._signal_sensor_dt or self._signal_progress_mono is None:
            # A new reading (or the first one we've seen) — re-arm.
            self._signal_sensor_dt = obs.sensor_dt
            self._signal_progress_mono = mono
            self._signal_fired = False
        if not prefs.signal_loss or self._signal_fired:
            return []
        limit_s = prefs.signal_loss_minutes * 60
        age_s = now - obs.sensor_dt.astimezone(UTC).timestamp()
        awake_s = mono - self._signal_progress_mono
        # Both clocks must agree: wall age alone would fire the moment the
        # Mac wakes from sleep, before the daemon had a chance to fetch.
        if age_s < limit_s or awake_s < limit_s:
            return []
        self._signal_fired = True
        minutes = int(age_s // 60)
        return [
            Alert(
                category=CATEGORY_SIGNAL,
                kind="signal_loss",
                title="No glucose data",
                body=(
                    f"No new reading for {_humanize_minutes(minutes)}. "
                    "Check your sensor, phone and connection."
                ),
            )
        ]


def _humanize_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minutes"
    hours, rem = divmod(minutes, 60)
    hours_text = "1 hour" if hours == 1 else f"{hours} hours"
    return hours_text if rem == 0 else f"{hours_text} {rem} min"
