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
import subprocess
import sys
import threading
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

# Why we use AppleScript (``osascript``) dialogs instead of ``rumps.Window`` /
# ``rumps.alert``:
#
# Unbundled Python processes started by launchd don't have a proper
# ``Info.plist`` with a ``CFBundleIdentifier``. Several AppKit UI APIs
# refuse to work in that context — ``rumps.notification`` is the
# documented example (see ``notifier.py``), and ``NSAlert.runModal`` /
# ``rumps.Window.run`` behave similarly: the call appears to start, but
# AppKit silently marks the process as misbehaving and ``launchd``
# tears it down a few seconds later. There's no crash report, just a
# clean exit and a relaunch — which from the user's perspective looks
# like "the menu went dead and nothing happened".
#
# ``osascript`` shells out to a properly bundled helper process
# (``System Events``) to host the dialog, so this works regardless of
# how our own process was launched. It's also the fallback the
# notification backend in ``notifier.py`` already uses, for the same
# underlying reason.
#
# Running the osascript subprocess on the main thread would block the
# rumps runloop for the whole time the dialog is up, freezing the
# menubar title. So the whole dialog flow runs on a background thread.
# Subprocess + keychain writes are safe off the main thread; the only
# thing that strictly isn't is UI mutation, and the next poll tick
# (which rumps drives on the main thread) picks up state changes.


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _osascript_dialog(
    *,
    title: str,
    message: str,
    default_text: str = "",
    ok: str = "OK",
    cancel: str = "Cancel",
    hidden_answer: bool = False,
) -> tuple[bool, str]:
    """Show a text-input dialog. Returns ``(clicked_ok, text)``.

    On cancel / close / error, returns ``(False, "")``.
    """
    parts = [
        f'display dialog "{_applescript_escape(message)}"',
        f'default answer "{_applescript_escape(default_text)}"',
        f'with title "{_applescript_escape(title)}"',
        f'buttons {{"{_applescript_escape(cancel)}", "{_applescript_escape(ok)}"}}',
        f'default button "{_applescript_escape(ok)}"',
        f'cancel button "{_applescript_escape(cancel)}"',
    ]
    if hidden_answer:
        parts.append("with hidden answer")
    # ``tell application "System Events"`` is what makes the dialog
    # reliably appear in front regardless of our own app-activation
    # state, which is exactly the problem we can't solve for unbundled
    # Python.
    script = (
        'tell application "System Events"\nactivate\n'
        + " ".join(parts)
        + "\nend tell"
    )
    proc = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        # Exit 1 + "-128" = user hit Cancel / closed the dialog.
        if "-128" in stderr or "User canceled" in stderr:
            return False, ""
        log.warning("osascript dialog failed: %s", stderr)
        return False, ""
    out = (proc.stdout or "").strip()
    # Format: "button returned:<ok>, text returned:<value>".
    text = ""
    marker = "text returned:"
    if marker in out:
        text = out[out.index(marker) + len(marker):].strip()
    return True, text


def _osascript_alert(*, title: str, message: str) -> None:
    """Show a one-button informational alert. Errors are logged, not raised."""
    script = (
        'tell application "System Events"\nactivate\n'
        f'display alert "{_applescript_escape(title)}" '
        f'message "{_applescript_escape(message)}"\n'
        "end tell"
    )
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:  # noqa: BLE001
        log.exception("osascript alert failed")


def _run_in_background(fn) -> None:
    """Run ``fn()`` on a daemon thread so the menu callback returns fast.

    Exceptions are logged, never propagated — a worker thread that dies
    with an uncaught exception is invisible to the user otherwise.
    """

    def _target() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("Background menu handler failed")

    threading.Thread(target=_target, daemon=True).start()


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


def _reading_menu_labels(reading: CurrentReading) -> tuple[str, str]:
    """Two menu rows: freshness (with sample clock in parens) and display units."""
    if reading.status in ("ok", "stale"):
        local_hhmm = _to_local_hhmm(reading.sensor_timestamp)
        age = _humanize_age(reading.reading_age_seconds)
        unit = reading.unit or ""
        if local_hhmm != "—":
            updated_line = f"Last Updated: {age} ({local_hhmm})"
        else:
            updated_line = f"Last Updated: {age}"
        unit_line = f"Units: {unit}" if unit else "Units: —"
        return (updated_line, unit_line)
    status = _STATUS_TEXT.get(reading.status, "Status: Unavailable")
    detail = _DETAIL_TEXT.get(reading.status, "")
    if detail:
        return (status, detail)
    return (status, "—")


def _humanize_age(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "Unknown"
    if age_seconds < 0:
        # Clock skew or bad payload timestamp; avoid showing confusing negatives.
        return "Just Now"
    if age_seconds < 60:
        return "Just Now"
    minutes = age_seconds // 60
    if minutes < 60:
        return "1 Min Ago" if minutes == 1 else f"{minutes} Min Ago"
    hours = minutes // 60
    return "1 Hour Ago" if hours == 1 else f"{hours} Hours Ago"


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
    "warming_up": "Sensor Warming Up",
    "sensor_ended": "Sensor Ended",
    "no_network": "No Internet Connection",
    "auth_failed": "Sign-In Required",
    "tos_required": "Open Libre App to Accept Terms",
    "rate_limited": "Temporarily Rate-Limited",
}

_DETAIL_TEXT = {
    "starting": "Getting First Reading",
    "warming_up": "Try Again in a Few Minutes",
    "sensor_ended": "Replace Sensor in Libre App",
    "no_network": "Check Wi-Fi and Try Refresh Now",
    "auth_failed": "Use Force Re-Authenticate",
    "tos_required": "Accept Prompts in Official Libre App",
    "rate_limited": "Will Retry Automatically",
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

        self._reading_updated_item = rumps.MenuItem("Connecting…")
        self._reading_unit_item = rumps.MenuItem("—")
        self._refresh_item = rumps.MenuItem("Refresh Now", callback=self._on_refresh)
        self._reauth_item = rumps.MenuItem("Force Re-Authenticate", callback=self._on_reauth)
        self._notifs_item = rumps.MenuItem(
            "Notifications", callback=self._on_toggle_notifications
        )
        self._notifs_item.state = 1 if (notifier and notifier.enabled) else 0
        self._creds_item = rumps.MenuItem(
            "Update Credentials…", callback=self._on_edit_credentials
        )
        self._thresholds_item = rumps.MenuItem(
            "Set Thresholds…", callback=self._on_edit_thresholds
        )
        self.menu = [
            self._reading_updated_item,
            self._reading_unit_item,
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
            self._reading_updated_item.title = "Background Service Offline"
            self._reading_unit_item.title = "Start the Daemon"
            return
        except DaemonError as e:
            log.warning("Daemon error: %s", e)
            self._set_title("⚠ daemon")
            self._reading_updated_item.title = "Background Service Error"
            self._reading_unit_item.title = "Try Force Re-Authenticate"
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

        u_line, units_line = _reading_menu_labels(reading)
        self._reading_updated_item.title = u_line
        self._reading_unit_item.title = units_line

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
            _run_in_background(
                lambda: _osascript_alert(
                    title="Notifications unavailable",
                    message="Notifier was not initialised. Restart the frontend.",
                )
            )
            return
        self._notifier.enabled = not self._notifier.enabled
        sender.state = 1 if self._notifier.enabled else 0
        log.info(
            "Notifications %s", "enabled" if self._notifier.enabled else "disabled"
        )

    def _on_edit_credentials(self, _sender) -> None:
        log.info("Update credentials clicked")
        _run_in_background(self._edit_credentials_impl)

    def _edit_credentials_impl(self) -> None:
        """Capture email + password with two dialogs and save to Keychain."""
        ok, email = _osascript_dialog(
            title="LibreLinkUp Email",
            message="Enter the email associated with your LibreLinkUp account.",
            ok="Next",
        )
        if not ok:
            return
        email = email.strip()
        if not email:
            return

        ok, password = _osascript_dialog(
            title="LibreLinkUp Password",
            message="Password is stored in the macOS Keychain, not on disk.",
            ok="Save",
            hidden_answer=True,
        )
        if not ok or not password:
            return

        try:
            save_credentials(email=email, password=password)
        except Exception as e:  # noqa: BLE001
            _osascript_alert(
                title="Could Not Save Credentials",
                message=f"{e}\n\nYou can still set LIBRELINKUP_EMAIL/_PASSWORD in .env.",
            )
            return
        _osascript_alert(
            title="Credentials Saved",
            message=(
                f"Stored under Keychain service '{KEYCHAIN_SERVICE}'. "
                "Click 'Force Re-Authenticate' to use them now."
            ),
        )

    def _on_edit_thresholds(self, _sender) -> None:
        log.info("Set thresholds clicked")
        _run_in_background(self._edit_thresholds_impl)

    def _edit_thresholds_impl(self) -> None:
        """Prompt for the three colour thresholds. Session-only; see below."""
        ok, response = _osascript_dialog(
            title="Glucose Thresholds (mg/dL)",
            message=(
                "Three numbers separated by commas: low, high, very_high.\n"
                f"Current: {int(self.thresholds.low)}, "
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
        )
        if not ok:
            return
        try:
            parts = [float(x.strip()) for x in response.split(",")]
            if len(parts) != 3:
                raise ValueError("expected three numbers")
            low, high, very_high = parts
            if not (low < high < very_high):
                raise ValueError("must satisfy low < high < very_high")
        except Exception as e:  # noqa: BLE001
            _osascript_alert(title="Invalid Thresholds", message=str(e))
            return

        # Mutating shared state from a worker thread: the frontend is
        # single-reader on the main thread (the poll tick), and these
        # attribute writes are atomic in CPython, so we don't need a
        # lock for correctness. Worst case: one tick uses old thresholds.
        self.thresholds = ColorThresholds(low=low, high=high, very_high=very_high)
        if self._notifier is not None:
            self._notifier.policy.low = low
            self._notifier.policy.high = high
            self._notifier.policy.very_high = very_high
        # The next rumps timer tick will re-render with the new thresholds
        # — we deliberately don't call ``_on_tick`` here because that
        # would touch AppKit UI off the main thread.


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
