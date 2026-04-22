"""Glucose-range notifications with hysteresis and a staleness alarm.

Why this lives here
-------------------
The daemon is deliberately dumb about what's "normal" for you — that
lives in the frontend's ``ColorThresholds``. It follows that the
notification policy also lives here, reusing the same thresholds so
colour and notification can never disagree.

Design goals
------------
* Notify on *transitions* into a concerning zone, not on every poll.
  30 pokes in 30 minutes for the same hypo is worse than one.
* Avoid chatter around the boundary. A value oscillating between
  178 ↔ 182 shouldn't fire on each tick — we apply a small hysteresis
  band so you only get notified when the value clearly re-enters a new
  zone.
* Optional staleness alarm — if the sensor stops reporting (e.g. you
  walked out of phone range, the sensor fell off), a single notification
  after ``stale_after_seconds`` is more useful than silence.

The ``NotificationPolicy`` class is pure: feed it readings, get back a
list of notifications to emit. The ``Notifier`` wrapper delivers them via
``MacOSNotificationBackend``: try ``rumps`` first (nice when bundled with a
proper ``Info.plist``), then fall back to ``osascript display notification``
so LaunchAgent / bare ``python -m macos_frontend`` still alerts without
hacking an ``Info.plist`` next to the venv interpreter.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

log = logging.getLogger(__name__)


class Zone(str, Enum):
    """The coarse zones we care about for notifications.

    Keep the set minimal: adding "in_range" notifications ('returned to
    normal') is tempting but gets noisy fast. We notify when things get
    worse; getting better is its own reward."""

    LOW = "low"
    IN_RANGE = "in_range"
    HIGH = "high"
    VERY_HIGH = "very_high"


@dataclass(frozen=True)
class NotificationEvent:
    """A message the caller should deliver now."""

    kind: str  # "threshold" | "stale"
    title: str
    subtitle: str | None
    message: str


def _zone_of(value_mg_dl: float, low: float, high: float, very_high: float) -> Zone:
    if value_mg_dl < low:
        return Zone.LOW
    if value_mg_dl > very_high:
        return Zone.VERY_HIGH
    if value_mg_dl > high:
        return Zone.HIGH
    return Zone.IN_RANGE


@dataclass
class NotificationPolicy:
    """Stateful decision-maker for glucose/staleness notifications.

    Separated from I/O so it's trivially unit-testable with a fake clock.

    Hysteresis: entering a worse zone requires the value to clear the
    plain threshold; returning to a calmer zone requires it to re-enter
    by ``hysteresis_mg_dl``. In practice this prevents "179/181/178/182"
    from firing two HIGH alerts in a row."""

    low: float
    high: float
    very_high: float
    hysteresis_mg_dl: float = 5.0
    cooldown_seconds: float = 15 * 60  # don't re-fire the same zone within 15 min
    stale_after_seconds: int = 10 * 60  # sensor lost / off-body alarm
    stale_cooldown_seconds: float = 30 * 60
    # Injected for tests. ``dataclass`` treats ``default=time.time`` as a
    # *value*, not a factory, so the field holds the function itself —
    # which is exactly what we want; callers replace it with a fake clock.
    _now: Callable[[], float] = field(default=time.time)
    _last_zone: Zone | None = field(default=None)
    _last_zone_notified_at: dict[Zone, float] = field(default_factory=dict)
    _last_stale_notified_at: float | None = field(default=None)

    def evaluate(
        self,
        *,
        value_mg_dl: float | None,
        reading_age_seconds: int | None,
        status: str,
    ) -> list[NotificationEvent]:
        """Classify the current reading and return any alerts to emit."""
        events: list[NotificationEvent] = []

        # Staleness: evaluated from the daemon-reported age, not our own
        # clock, so clock skew between frontend and daemon doesn't matter.
        if (
            reading_age_seconds is not None
            and reading_age_seconds >= self.stale_after_seconds
            and self._cooldown_elapsed(self._last_stale_notified_at, self.stale_cooldown_seconds)
        ):
            events.append(
                NotificationEvent(
                    kind="stale",
                    title="Glucose data is stale",
                    subtitle=None,
                    message=(
                        f"No new reading for {reading_age_seconds // 60} min. "
                        "Check the sensor is still paired with your phone."
                    ),
                )
            )
            self._last_stale_notified_at = self._now()

        # Threshold alarms only when we have a current numeric value and
        # the daemon thinks it's fresh. Stale/no_network shouldn't spam.
        if value_mg_dl is None or status not in ("ok",):
            return events

        new_zone = self._compute_zone_with_hysteresis(value_mg_dl)
        if new_zone != self._last_zone and new_zone in (
            Zone.LOW,
            Zone.HIGH,
            Zone.VERY_HIGH,
        ):
            last_fired = self._last_zone_notified_at.get(new_zone)
            if self._cooldown_elapsed(last_fired, self.cooldown_seconds):
                events.append(self._build_threshold_event(new_zone, value_mg_dl))
                self._last_zone_notified_at[new_zone] = self._now()
        self._last_zone = new_zone
        return events

    # --- internals ---

    def _cooldown_elapsed(self, last_at: float | None, cooldown: float) -> bool:
        if last_at is None:
            return True
        return (self._now() - last_at) >= cooldown

    def _compute_zone_with_hysteresis(self, value: float) -> Zone:
        """Same as ``_zone_of`` but with a buffer to exit a worse zone.

        Concretely: once we're in LOW, we only leave at ``low + h``; once
        we're in HIGH/VERY_HIGH, we only drop down when value ``<=
        threshold - h``. Entering the worse zone always uses the plain
        threshold — we don't want to delay hypo alerts.
        """
        last = self._last_zone
        h = self.hysteresis_mg_dl

        if last == Zone.LOW:
            return Zone.LOW if value < self.low + h else _zone_of(
                value, self.low, self.high, self.very_high
            )
        if last == Zone.VERY_HIGH:
            return Zone.VERY_HIGH if value > self.very_high - h else _zone_of(
                value, self.low, self.high, self.very_high
            )
        if last == Zone.HIGH:
            # Could drop to IN_RANGE or escalate to VERY_HIGH.
            if value > self.very_high:
                return Zone.VERY_HIGH
            return Zone.HIGH if value > self.high - h else _zone_of(
                value, self.low, self.high, self.very_high
            )
        return _zone_of(value, self.low, self.high, self.very_high)

    def _build_threshold_event(self, zone: Zone, value: float) -> NotificationEvent:
        rounded = int(round(value))
        if zone == Zone.LOW:
            return NotificationEvent(
                kind="threshold",
                title=f"Low glucose — {rounded} mg/dL",
                subtitle="Consider treating",
                message=f"Below your low threshold of {int(self.low)} mg/dL.",
            )
        if zone == Zone.HIGH:
            return NotificationEvent(
                kind="threshold",
                title=f"High glucose — {rounded} mg/dL",
                subtitle=None,
                message=f"Above your high threshold of {int(self.high)} mg/dL.",
            )
        return NotificationEvent(
            kind="threshold",
            title=f"Very high glucose — {rounded} mg/dL",
            subtitle="Check for ketones if symptomatic",
            message=f"Above your very-high threshold of {int(self.very_high)} mg/dL.",
        )


# --- side-effect wrapper ---------------------------------------------------


class NotificationBackend(Protocol):
    def notify(self, *, title: str, subtitle: str | None, message: str) -> None: ...


def _applescript_escape(s: str) -> str:
    """Escape a string for use inside AppleScript double-quoted text."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _notify_via_applescript(
    *, title: str, subtitle: str | None, message: str
) -> None:
    """``osascript`` path — works without ``CFBundleIdentifier`` (LaunchAgent)."""
    body = _applescript_escape(message)
    t = _applescript_escape(title)
    if subtitle:
        sub = _applescript_escape(subtitle)
        script = (
            f'display notification "{body}" with title "{t}" subtitle "{sub}"'
        )
    else:
        script = f'display notification "{body}" with title "{t}"'
    proc = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"osascript exited {proc.returncode}")


class MacOSNotificationBackend:
    """Try rumps (rich Notification Center integration), then AppleScript.

    Unbundled Python processes started by launchd lack an ``Info.plist`` with
    ``CFBundleIdentifier``, which ``rumps.notification`` requires. The
    ``osascript`` fallback uses the same user-visible banners without
    installing anything beside ``.venv/bin/python``."""

    def notify(self, *, title: str, subtitle: str | None, message: str) -> None:
        err_rumps: Exception | None = None
        try:
            import rumps  # imported lazily so tests don't need AppKit

            rumps.notification(title=title, subtitle=subtitle or "", message=message)
            return
        except Exception as e:  # noqa: BLE001
            err_rumps = e
            log.debug("rumps notification unavailable, trying osascript: %s", e)
        try:
            _notify_via_applescript(title=title, subtitle=subtitle, message=message)
        except Exception as e2:  # noqa: BLE001
            log.warning(
                "Could not deliver notification (%s): rumps=%s; osascript=%s",
                title,
                err_rumps,
                e2,
            )


class Notifier:
    """Bridges ``NotificationPolicy`` and a delivery backend."""

    def __init__(
        self,
        policy: NotificationPolicy,
        *,
        backend: NotificationBackend | None = None,
        enabled: bool = True,
    ) -> None:
        self.policy = policy
        self.enabled = enabled
        self._backend = backend or MacOSNotificationBackend()

    def handle(
        self,
        *,
        value_mg_dl: float | None,
        reading_age_seconds: int | None,
        status: str,
    ) -> list[NotificationEvent]:
        events = self.policy.evaluate(
            value_mg_dl=value_mg_dl,
            reading_age_seconds=reading_age_seconds,
            status=status,
        )
        if self.enabled:
            for ev in events:
                self._backend.notify(
                    title=ev.title, subtitle=ev.subtitle, message=ev.message
                )
        return events
