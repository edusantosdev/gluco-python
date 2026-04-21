"""Phase 1 MVP — monolithic menubar glucose display.

A single-file rumps app that polls LibreLinkUp directly and updates the
macOS menubar title with the current glucose value and trend arrow.

This is deliberately a monolith. The daemon split is Phase 2; for now we
just want to confirm a working menubar experience with live data.

Run:
    .venv/bin/python mvp.py
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
from pylibrelinkup import (
    APIUrl,
    AuthenticationError,
    LLUAPIRateLimitError,
    PrivacyPolicyError,
    PyLibreLinkUp,
    TermsOfUseError,
)

from glucose_common.regions import resolve_librelinkup_region
from glucose_common.trend import trend_symbol
from glucose_common.units import glucose_unit_label

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("glucose.mvp")

# --- Menubar glyphs for each known failure mode. Phase 2 will replace
# these with a richer Status enum surfaced by the daemon.
GLYPH_STARTING = "···"
GLYPH_AUTH = "⚠ auth"
GLYPH_TOS = "⚠ ToS"
GLYPH_RATE = "⚠ rate"
GLYPH_NET = "⚠ net"
GLYPH_UNKNOWN = "⚠"


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
    if value_mg_dl < t.low:
        return NSColor.systemRedColor()
    if value_mg_dl > t.very_high:
        return NSColor.systemOrangeColor()
    if value_mg_dl > t.high:
        return NSColor.systemYellowColor()
    return NSColor.systemGreenColor()


class GlucoseApp(rumps.App):
    """Menubar app that polls LibreLinkUp on a timer.

    The rumps event loop runs on the macOS main thread. The polling callback
    runs there too, via ``rumps.Timer``, which means network I/O briefly
    blocks the UI — acceptable for a 1s-ish HTTPS call every 60 seconds,
    and the daemon split in Phase 2 makes the point moot anyway.
    """

    def __init__(
        self,
        email: str,
        password: str,
        region: APIUrl,
        poll_interval: int,
        thresholds: ColorThresholds,
    ):
        super().__init__(GLYPH_STARTING, quit_button="Quit")
        self.poll_interval = poll_interval
        self.thresholds = thresholds

        self._last_updated_item = rumps.MenuItem("Last updated: never")
        self._status_item = rumps.MenuItem("Status: starting…")
        self._refresh_item = rumps.MenuItem("Refresh now", callback=self._on_refresh)
        self.menu = [self._status_item, self._last_updated_item, None, self._refresh_item]

        self.client = PyLibreLinkUp(email=email, password=password, api_url=region)
        self.patient = None

        if self._authenticate():
            self._load_patient()

        # Explicit Timer — avoids the @rumps.timer decorator's interval
        # mutation trick, which relies on rumps internals.
        self._timer = rumps.Timer(self._on_tick, poll_interval)
        self._timer.start()
        # First fetch immediately so the user doesn't wait a full interval.
        self._on_tick(None)

    # --- setup helpers ---

    def _authenticate(self) -> bool:
        try:
            self.client.authenticate()
            log.info("Authenticated with LibreLinkUp")
            return True
        except (TermsOfUseError, PrivacyPolicyError):
            log.error("ToS/Privacy re-accept required in the official Libre app")
            self._set_error(GLYPH_TOS, "Action required: open Libre app on phone")
        except AuthenticationError as e:
            log.error("Auth failed: %s", e)
            self._set_error(GLYPH_AUTH, "Authentication failed — check credentials")
        except LLUAPIRateLimitError as e:
            retry = getattr(e, "retry_after", "?")
            log.warning("Rate limited on auth; retry after %ss", retry)
            self._set_error(GLYPH_RATE, f"Rate limited (retry after {retry}s)")
        except Exception as e:  # noqa: BLE001 — last-resort catch-all
            log.exception("Unexpected auth error")
            self._set_error(GLYPH_NET, f"{type(e).__name__}: {e}")
        return False

    def _load_patient(self) -> None:
        try:
            patients = self.client.get_patients()
        except Exception as e:  # noqa: BLE001
            log.exception("get_patients failed")
            self._set_error(GLYPH_NET, f"{type(e).__name__}: {e}")
            return
        if not patients:
            log.error("No patients shared with this account")
            self._set_error(GLYPH_AUTH, "No patients shared with this account")
            return
        self.patient = patients[0]
        log.info("Patient: %s %s", self.patient.first_name, self.patient.last_name)

    # --- tick handlers ---

    def _on_tick(self, _sender) -> None:
        if self.patient is None:
            # Couldn't load patient on startup; try again now in case it was
            # a transient network issue.
            if not self._authenticate():
                return
            self._load_patient()
            if self.patient is None:
                return

        try:
            reading = self.client.latest(self.patient)
        except LLUAPIRateLimitError as e:
            retry = getattr(e, "retry_after", "?")
            log.warning("Rate limited; retry after %ss", retry)
            self._set_error(GLYPH_RATE, f"Rate limited (retry after {retry}s)")
            return
        except (TermsOfUseError, PrivacyPolicyError):
            log.warning("ToS/Privacy re-accept required")
            self._set_error(GLYPH_TOS, "Action required: open Libre app on phone")
            return
        except AuthenticationError:
            log.info("Auth expired; re-authenticating")
            self.patient = None
            if self._authenticate():
                self._load_patient()
            return
        except Exception as e:  # noqa: BLE001
            log.warning("latest() failed: %s: %s", type(e).__name__, e)
            self._set_error(GLYPH_NET, f"{type(e).__name__}: {e}")
            return

        try:
            value = int(round(reading.value))
        except (TypeError, ValueError):
            value = reading.value
        title = f"{value} {trend_symbol(reading.trend)}".strip()
        value_for_color = getattr(reading, "value_in_mg_per_dl", None)
        color = None
        if value_for_color is not None:
            color = _color_for(float(value_for_color), self.thresholds)
        self._set_title(title, color=color)
        self._status_item.title = f"Status: ok ({glucose_unit_label(reading.glucose_units)})"
        self._last_updated_item.title = (
            f"Last updated: {reading.timestamp.strftime('%H:%M')}"
        )

    def _on_refresh(self, _sender) -> None:
        log.info("Manual refresh")
        self._on_tick(None)

    # --- UI helpers ---

    def _set_title(self, text: str, color=None) -> None:
        nsapp = getattr(self, "_nsapp", None)
        if nsapp is None or not hasattr(nsapp, "nsstatusitem"):
            self.title = text
            return
        button = nsapp.nsstatusitem.button()
        if button is None:
            self.title = text
            return
        if color is None:
            color = NSColor.labelColor()
        attrs = {
            NSForegroundColorAttributeName: color,
            NSFontAttributeName: NSFont.menuBarFontOfSize_(0),
        }
        try:
            attr_string = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            button.setAttributedTitle_(attr_string)
        except Exception:  # noqa: BLE001
            self.title = text

    def _set_error(self, glyph: str, status_line: str) -> None:
        self._set_title(glyph)
        self._status_item.title = f"Status: {status_line}"


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")

    email = os.environ.get("LIBRELINKUP_EMAIL")
    password = os.environ.get("LIBRELINKUP_PASSWORD")
    if not email or not password:
        print(
            "LIBRELINKUP_EMAIL / LIBRELINKUP_PASSWORD not set. "
            "Copy .env.example to .env and fill it in.",
            file=sys.stderr,
        )
        return 1

    region_raw = os.environ.get("LIBRELINKUP_REGION", "US")
    region, region_hint = resolve_librelinkup_region(region_raw)
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
    thresholds = ColorThresholds.from_env()

    if region_hint:
        log.info("%s", region_hint)
    log.info(
        "Starting MVP (region=%s, poll=%ss, thresholds: <%g red, %g–%g green, "
        "%g–%g yellow, >%g orange)",
        region.name,
        poll_interval,
        thresholds.low,
        thresholds.low,
        thresholds.high,
        thresholds.high,
        thresholds.very_high,
        thresholds.very_high,
    )
    GlucoseApp(
        email=email,
        password=password,
        region=region,
        poll_interval=poll_interval,
        thresholds=thresholds,
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
