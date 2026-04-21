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
from datetime import datetime
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

from glucose_common.credentials import (  # noqa: E402
    KEYCHAIN_SERVICE,
    save_credentials,
)
from macos_frontend.api_client import (  # noqa: E402
    CurrentReading,
    DaemonClient,
    DaemonError,
    DaemonUnreachable,
)
from macos_frontend.notifier import NotificationPolicy, Notifier  # noqa: E402

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
    if reading.status in ("ok", "stale"):
        return f"Updated: {_humanize_age(reading.reading_age_seconds)}"
    return _STATUS_TEXT.get(reading.status, "Status: unavailable")


def _format_detail_line(reading: CurrentReading) -> str:
    local_hhmm = _to_local_hhmm(reading.sensor_timestamp)
    unit = reading.unit or ""
    if local_hhmm != "—":
        if unit:
            return f"Last reading: {local_hhmm} ({unit})"
        return f"Last reading: {local_hhmm}"
    if reading.status in ("ok", "stale"):
        return "Last reading: —"
    return _DETAIL_TEXT.get(reading.status, "Waiting for next update")


def _humanize_age(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "unknown"
    if age_seconds < 0:
        # Clock skew or bad payload timestamp; avoid showing confusing negatives.
        return "just now"
    if age_seconds < 60:
        return "just now"
    minutes = age_seconds // 60
    if minutes < 60:
        return "1 min ago" if minutes == 1 else f"{minutes} min ago"
    hours = minutes // 60
    return "1 hour ago" if hours == 1 else f"{hours} hours ago"


def _to_local_hhmm(timestamp_iso: str | None) -> str:
    if not timestamp_iso:
        return "—"
    try:
        # Daemon sends UTC with trailing Z.
        dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return "—"


_STATUS_TEXT = {
    "starting": "Connecting…",
    "warming_up": "Sensor warming up",
    "sensor_ended": "Sensor ended",
    "no_network": "No internet connection",
    "auth_failed": "Sign-in required",
    "tos_required": "Open Libre app to accept terms",
    "rate_limited": "Temporarily rate-limited",
}

_DETAIL_TEXT = {
    "starting": "Getting first reading",
    "warming_up": "Try again in a few minutes",
    "sensor_ended": "Replace sensor in Libre app",
    "no_network": "Check Wi-Fi and try Refresh now",
    "auth_failed": "Use Force re-authenticate",
    "tos_required": "Accept prompts in official Libre app",
    "rate_limited": "Will retry automatically",
}


class GlucoseApp(rumps.App):
    def __init__(
        self,
        base_url: str,
        poll_interval: int,
        thresholds: ColorThresholds,
        notifier: Notifier | None = None,
    ):
        super().__init__("···", quit_button="Quit")
        self.poll_interval = poll_interval
        self.thresholds = thresholds
        self._notifier = notifier

        self._status_item = rumps.MenuItem("Status: starting…")
        self._sensor_item = rumps.MenuItem("Sensor: —")
        self._refresh_item = rumps.MenuItem("Refresh now", callback=self._on_refresh)
        self._reauth_item = rumps.MenuItem("Force re-authenticate", callback=self._on_reauth)
        self._notifs_item = rumps.MenuItem(
            "Notifications", callback=self._on_toggle_notifications
        )
        self._notifs_item.state = 1 if (notifier and notifier.enabled) else 0
        self._creds_item = rumps.MenuItem(
            "Update credentials…", callback=self._on_edit_credentials
        )
        self._thresholds_item = rumps.MenuItem(
            "Set thresholds…", callback=self._on_edit_thresholds
        )
        self.menu = [
            self._status_item,
            self._sensor_item,
            None,
            self._refresh_item,
            self._reauth_item,
            None,
            self._notifs_item,
            self._creds_item,
            self._thresholds_item,
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
            self._status_item.title = "Background service is offline"
            self._sensor_item.title = "Start daemon first"
            return
        except DaemonError as e:
            log.warning("Daemon error: %s", e)
            self._set_title("⚠ daemon")
            self._status_item.title = "Background service error"
            self._sensor_item.title = "Try Force re-authenticate"
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
        self._sensor_item.title = _format_detail_line(reading)

        if self._notifier is not None:
            self._notifier.handle(
                value_mg_dl=reading.value_in_mg_per_dl,
                reading_age_seconds=reading.reading_age_seconds,
                status=reading.status,
            )

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

    # --- Preferences ---

    def _on_toggle_notifications(self, sender) -> None:
        if self._notifier is None:
            rumps.alert(
                title="Notifications unavailable",
                message="Notifier was not initialised. Restart the frontend.",
            )
            return
        self._notifier.enabled = not self._notifier.enabled
        sender.state = 1 if self._notifier.enabled else 0
        log.info(
            "Notifications %s", "enabled" if self._notifier.enabled else "disabled"
        )

    def _on_edit_credentials(self, _sender) -> None:
        """Capture email + password with two prompts and save to Keychain.

        rumps has no native multi-field form. Two consecutive Windows is
        a bit crude but lets us use ``secure=True`` on the password one,
        which matters more than having everything on one screen."""
        email_window = rumps.Window(
            title="LibreLinkUp email",
            message="Enter the email associated with your LibreLinkUp account.",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 22),
        )
        result = email_window.run()
        if not result.clicked or not result.text.strip():
            return
        email = result.text.strip()

        pw_window = rumps.Window(
            title="LibreLinkUp password",
            message="Password is stored in the macOS Keychain, not on disk.",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 22),
        )
        # rumps doesn't expose secure text entry directly; the NSSecureTextField
        # attribute is set by passing ``secure=True`` in newer versions. Older
        # rumps ignores unknown kwargs, so this degrades gracefully.
        try:
            pw_window.secure = True  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        pw_result = pw_window.run()
        if not pw_result.clicked or not pw_result.text:
            return
        try:
            save_credentials(email=email, password=pw_result.text)
        except Exception as e:  # noqa: BLE001
            rumps.alert(
                title="Could not save credentials",
                message=f"{e}\n\nYou can still set LIBRELINKUP_EMAIL/_PASSWORD in .env.",
            )
            return
        rumps.alert(
            title="Credentials saved",
            message=(
                f"Stored under Keychain service '{KEYCHAIN_SERVICE}'. "
                "Click 'Force re-authenticate' to use them now."
            ),
        )

    def _on_edit_thresholds(self, _sender) -> None:
        """Prompt for the three colour thresholds. Session-only; see below."""
        window = rumps.Window(
            title="Glucose thresholds (mg/dL)",
            message=(
                "Three numbers separated by commas: low, high, very_high.\n"
                "Current: "
                f"{int(self.thresholds.low)}, "
                f"{int(self.thresholds.high)}, "
                f"{int(self.thresholds.very_high)}.\n\n"
                "To persist across restarts, set THRESHOLD_LOW_MG_DL, "
                "THRESHOLD_HIGH_MG_DL, THRESHOLD_VERY_HIGH_MG_DL in .env."
            ),
            default_text=(
                f"{int(self.thresholds.low)},"
                f"{int(self.thresholds.high)},"
                f"{int(self.thresholds.very_high)}"
            ),
            ok="Apply",
            cancel="Cancel",
            dimensions=(320, 22),
        )
        result = window.run()
        if not result.clicked:
            return
        try:
            parts = [float(x.strip()) for x in result.text.split(",")]
            if len(parts) != 3:
                raise ValueError("expected three numbers")
            low, high, very_high = parts
            if not (low < high < very_high):
                raise ValueError("must satisfy low < high < very_high")
        except Exception as e:  # noqa: BLE001
            rumps.alert(title="Invalid thresholds", message=str(e))
            return

        self.thresholds = ColorThresholds(low=low, high=high, very_high=very_high)
        if self._notifier is not None:
            self._notifier.policy.low = low
            self._notifier.policy.high = high
            self._notifier.policy.very_high = very_high
        self._on_tick(None)


def _build_notifier(thresholds: ColorThresholds) -> Notifier:
    stale_after = int(os.environ.get("NOTIFY_STALE_AFTER_SECONDS", str(10 * 60)))
    cooldown = int(os.environ.get("NOTIFY_COOLDOWN_SECONDS", str(15 * 60)))
    enabled = os.environ.get("NOTIFY_ENABLED", "1") not in ("0", "false", "False", "")
    policy = NotificationPolicy(
        low=thresholds.low,
        high=thresholds.high,
        very_high=thresholds.very_high,
        cooldown_seconds=cooldown,
        stale_after_seconds=stale_after,
    )
    return Notifier(policy, enabled=enabled)


def main() -> int:
    load_dotenv(_REPO_ROOT / ".env")
    base_url = os.environ.get("DAEMON_URL", "http://127.0.0.1:8765")
    poll_interval = int(os.environ.get("FRONTEND_POLL_SECONDS", "30"))
    thresholds = ColorThresholds.from_env()
    notifier = _build_notifier(thresholds)
    log.info(
        "Starting frontend (daemon=%s, poll=%ss, notifications=%s, "
        "thresholds: <%g red, %g–%g green, %g–%g yellow, >%g orange)",
        base_url,
        poll_interval,
        "on" if notifier.enabled else "off",
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
        notifier=notifier,
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
