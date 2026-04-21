"""Phase 2 macOS frontend — a thin rumps wrapper around the daemon HTTP API.

This module knows nothing about LibreLinkUp, credentials, regions, or
trend codes. It maps daemon ``Status`` values to menubar glyphs and shows
the current value when the status allows it. That's the whole job.

Run::

    .venv/bin/python -m macos_frontend
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import rumps
from AppKit import (
    NSAttributedString,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
)
from dotenv import load_dotenv

# Allow ``python -m macos_frontend`` and ``python macos_frontend/app.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from macos_frontend.api_client import (  # noqa: E402
    CurrentReading,
    DaemonClient,
    DaemonError,
    DaemonUnreachable,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("glucose.frontend")


# Map daemon Status values to a single-character / short-text menubar title
# for the *non-ok* cases. When status == "ok", we render the value directly.
_STATUS_GLYPH = {
    "starting": "···",
    "stale": "?",  # value is still shown alongside this
    "warming_up": "◴",
    "sensor_ended": "⚠ sensor",
    "no_network": "⚠ net",
    "auth_failed": "⚠ auth",
    "tos_required": "⚠ ToS",
    "rate_limited": "⚠ rate",
}


def _format_title(reading: CurrentReading) -> str:
    if reading.status == "ok" and reading.value is not None:
        arrow = reading.trend_symbol or ""
        return f"{int(round(reading.value))} {arrow}".strip()
    if reading.status == "stale" and reading.value is not None:
        # Keep the last known value visible but mark it as stale with a trailing *
        arrow = reading.trend_symbol or ""
        return f"{int(round(reading.value))} {arrow}*".strip()
    return _STATUS_GLYPH.get(reading.status, "⚠")


# --- Color thresholds -------------------------------------------------------
# All thresholds are in mg/dL. The frontend always uses
# ``value_in_mg_per_dl`` from the daemon for color decisions, so users on
# mmol/L still get correct coloring.
#
# Boundary semantics chosen to be friendly to the user when sitting on a
# round number:
#   value <  low         → red    (e.g. 69 = red, 70 = green)
#   value <= high        → green  (e.g. 180 = green, 180.1 = yellow)
#   value <= very_high   → yellow (e.g. 250 = yellow, 250.1 = orange)
#   value >  very_high   → orange


@dataclass(frozen=True)
class ColorThresholds:
    low: float
    high: float
    very_high: float

    @classmethod
    def from_env(cls) -> ColorThresholds:
        return cls(
            low=float(os.environ.get("THRESHOLD_LOW_MG_DL", "70")),
            high=float(os.environ.get("THRESHOLD_HIGH_MG_DL", "180")),
            very_high=float(os.environ.get("THRESHOLD_VERY_HIGH_MG_DL", "250")),
        )


def _color_for(value_mg_dl: float, t: ColorThresholds):
    """Return an ``NSColor`` for the menubar title, or ``None`` for default."""
    if value_mg_dl < t.low:
        return NSColor.systemRedColor()
    if value_mg_dl > t.very_high:
        return NSColor.systemOrangeColor()
    if value_mg_dl > t.high:
        return NSColor.systemYellowColor()
    return NSColor.systemGreenColor()


def _format_status_line(reading: CurrentReading) -> str:
    if reading.status == "ok":
        age = reading.reading_age_seconds
        return f"Status: ok ({reading.unit or ''}) — sensor age {age}s"
    if reading.status == "stale":
        age = reading.reading_age_seconds
        return f"Status: stale — sensor age {age}s"
    return f"Status: {reading.status.replace('_', ' ')}"


class GlucoseApp(rumps.App):
    def __init__(self, base_url: str, poll_interval: int, thresholds: ColorThresholds):
        super().__init__("···", quit_button="Quit")
        self.poll_interval = poll_interval
        self.thresholds = thresholds

        self._status_item = rumps.MenuItem("Status: starting…")
        self._sensor_item = rumps.MenuItem("Sensor: —")
        self._refresh_item = rumps.MenuItem("Refresh now", callback=self._on_refresh)
        self._reauth_item = rumps.MenuItem("Force re-authenticate", callback=self._on_reauth)
        self.menu = [
            self._status_item,
            self._sensor_item,
            None,
            self._refresh_item,
            self._reauth_item,
        ]

        self._client = DaemonClient(base_url=base_url)

        # Explicit Timer rather than @rumps.timer — same reasoning as in mvp.py.
        self._timer = rumps.Timer(self._on_tick, poll_interval)
        self._timer.start()
        self._on_tick(None)

    # --- title rendering ---

    def _set_title(self, text: str, color=None) -> None:
        """Set the menubar title.

        rumps's ``self.title = ...`` only handles plain text. To colour the
        text we set the underlying ``NSStatusBarButton.attributedTitle``
        with an ``NSAttributedString``. We *always* go through this helper
        (color=None falls back to the default label colour) so the two
        title channels never conflict — mixing them leaves stale colour
        from a previous tick visible.
        """
        nsapp = getattr(self, "_nsapp", None)
        if nsapp is None or not hasattr(nsapp, "nsstatusitem"):
            # Pre-runloop tick (very early in __init__) — fall back to plain.
            self.title = text
            return
        button = nsapp.nsstatusitem.button()
        if button is None:
            self.title = text
            return
        if color is None:
            color = NSColor.labelColor()
        # Cocoa expects specific attribute-name constants; generic string keys
        # are ignored, which leaves the text in default monochrome.
        attrs = {
            NSForegroundColorAttributeName: color,
            NSFontAttributeName: NSFont.menuBarFontOfSize_(0),
        }
        try:
            attr_string = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            button.setAttributedTitle_(attr_string)
        except Exception:  # noqa: BLE001
            # Never crash UI updates over attributed-title quirks.
            self.title = text

    # --- timer / menu callbacks ---

    def _on_tick(self, _sender) -> None:
        try:
            reading = self._client.current()
        except DaemonUnreachable as e:
            log.warning("Daemon unreachable: %s", e)
            self._set_title("⚠ daemon")
            self._status_item.title = "Status: daemon unreachable"
            return
        except DaemonError as e:
            log.warning("Daemon error: %s", e)
            self._set_title("⚠ daemon")
            self._status_item.title = f"Status: daemon error — {e}"
            return

        title = _format_title(reading)
        # Only colour when we actually have a numeric value to evaluate.
        # Stale readings still get coloured (the value is the most recent
        # known truth), error glyphs use default text colour.
        color = None
        value_for_color = reading.value_in_mg_per_dl
        if reading.status in ("ok", "stale") and value_for_color is not None:
            color = _color_for(value_for_color, self.thresholds)
        self._set_title(title, color=color)

        self._status_item.title = _format_status_line(reading)
        if reading.sensor_timestamp:
            # ISO 8601 with a 'T' separator; show just HH:MM for the menu.
            try:
                hhmm = reading.sensor_timestamp[11:16]
            except (TypeError, IndexError):
                hhmm = "—"
            self._sensor_item.title = f"Sensor: {hhmm}"

    def _on_refresh(self, _sender) -> None:
        log.info("Manual refresh")
        self._on_tick(None)

    def _on_reauth(self, _sender) -> None:
        log.info("Force re-authenticate")
        try:
            self._client.force_refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("Re-auth failed: %s", e)
        self._on_tick(None)


def main() -> int:
    load_dotenv(_REPO_ROOT / ".env")
    base_url = os.environ.get("DAEMON_URL", "http://127.0.0.1:8765")
    poll_interval = int(os.environ.get("FRONTEND_POLL_SECONDS", "30"))
    thresholds = ColorThresholds.from_env()
    log.info(
        "Starting frontend (daemon=%s, poll=%ss, thresholds: <%g red, %g–%g green, "
        "%g–%g yellow, >%g orange)",
        base_url,
        poll_interval,
        thresholds.low,
        thresholds.low,
        thresholds.high,
        thresholds.high,
        thresholds.very_high,
        thresholds.very_high,
    )
    GlucoseApp(
        base_url=base_url,
        poll_interval=poll_interval,
        thresholds=thresholds,
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
