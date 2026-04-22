"""macOS menubar frontend — a thin rumps wrapper around the daemon HTTP API.

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

import objc
import rumps
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApp,
    NSAttributedString,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSSecureTextField,
    NSStackView,
    NSTextField,
    NSUserInterfaceLayoutOrientationVertical,
)
from dotenv import load_dotenv
from Foundation import NSBundle, NSObject

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
    GraphPoint,
)
from macos_frontend.graph_view import GlucoseGraphView, GraphThresholds  # noqa: E402
from macos_frontend.notifier import NotificationPolicy, Notifier  # noqa: E402

# Graph appears at the top of the dropdown above the freshness/units rows.
# 8 hours is the sweet spot between "see today's pattern" and "fits in a
# menu item without dominating the screen". Width is wide enough to make
# minute-resolution shape visible but not so wide that the menu overflows.
_GRAPH_HOURS = 8
_GRAPH_WIDTH = 320
_GRAPH_HEIGHT = 110

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("glucose.frontend")

# Dialog strategy: native NSAlert when bundled, osascript fallback otherwise.
#
# When launched from launchd via the ``Glucose Menubar.app`` wrapper
# bundle (see ``scripts/app_bundle/``), AppKit sees a real
# ``CFBundleIdentifier`` and ``LSUIElement`` and modal dialogs work
# correctly — so we show a single ``NSAlert`` with a two-field
# accessory view for credentials, which is the UX the user asked for.
#
# When running unbundled (e.g. ``python -m macos_frontend`` from a
# terminal during development), the Python process has no bundle
# identity. Several AppKit UI APIs refuse to work in that context —
# ``rumps.notification`` is the documented example (see
# ``notifier.py``), and ``NSAlert.runModal`` / ``rumps.Window.run``
# behave similarly: the call appears to start, but AppKit silently
# marks the process as misbehaving and ``launchd`` tears it down a
# few seconds later. There's no crash report, just a clean exit and a
# relaunch — which from the user's perspective looks like "the menu
# went dead and nothing happened". In that mode we fall back to
# ``osascript``, which shells out to a properly bundled helper
# process (``System Events``) to host the dialog. That's also the
# fallback the notification backend uses, for the same reason.
#
# Either way, the dialog flow runs on a background thread so the
# rumps menu callback returns immediately (otherwise the menu stays
# wedged in its tracking session). The native-NSAlert path uses
# ``performSelectorOnMainThread:`` to marshal the actual UI call back
# to the main thread.


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
    # Run ``display dialog`` directly (no ``tell application "System Events"``
    # wrapper). The SE wrapper isn't needed here — osascript itself is a
    # proper bundled helper — and empirically it can interfere with the
    # second invocation from the same process (the hidden-answer password
    # prompt never appears). Using ``tell me to activate`` brings
    # osascript's own dialog to the front.
    script = "tell me to activate\n" + " ".join(parts)
    log.info("osascript dialog: title=%r hidden=%s", title, hidden_answer)
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
            log.info("osascript dialog %r: cancelled", title)
            return False, ""
        log.warning("osascript dialog %r failed (rc=%s): %s",
                    title, proc.returncode, stderr)
        return False, ""
    out = (proc.stdout or "").strip()
    # Format: "button returned:<ok>, text returned:<value>".
    text = ""
    marker = "text returned:"
    if marker in out:
        text = out[out.index(marker) + len(marker):].strip()
    log.info("osascript dialog %r: ok, text_len=%d", title, len(text))
    return True, text


def _osascript_alert(*, title: str, message: str) -> None:
    """Show a one-button informational alert. Errors are logged, not raised."""
    script = (
        "tell me to activate\n"
        f'display alert "{_applescript_escape(title)}" '
        f'message "{_applescript_escape(message)}"'
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


# --- bundled-mode detection ------------------------------------------------
#
# When launched via the ``Glucose Menubar.app`` wrapper bundle, AppKit sees
# a real ``CFBundleIdentifier`` and ``NSAlert.runModal()`` works correctly.
# When launched directly (``python -m macos_frontend`` during development),
# the same calls can silently tear down the process — so we fall back to
# the osascript two-dialog flow.

_BUNDLE_ID = "com.glucose-display.frontend"


def _is_bundled() -> bool:
    try:
        bid = NSBundle.mainBundle().bundleIdentifier()
    except Exception:  # noqa: BLE001
        return False
    return bid == _BUNDLE_ID


# --- main-thread dispatch --------------------------------------------------
#
# AppKit UI calls (NSAlert.runModal, NSApp.activateIgnoringOtherApps_, etc.)
# must happen on the main thread. The dialog flow runs on a background
# thread (so the rumps menu callback returns immediately and the menu
# doesn't stay wedged in its tracking session), so we need a way to ask
# the main thread to run a block and wait for it.
#
# ``performSelectorOnMainThread:withObject:waitUntilDone:`` is the
# blessed PyObjC idiom for this — NSRunLoop processes the selector in
# ``NSDefaultRunLoopMode`` as soon as the menu tracking ends.


class _MainThreadRunner(NSObject):
    """Objective-C proxy that invokes a Python callable on the main thread."""

    def initWithCallable_(self, fn):  # noqa: N802  - Obj-C naming.
        self = objc.super(_MainThreadRunner, self).init()
        if self is None:
            return None
        self._fn = fn
        self._result = None
        self._error: BaseException | None = None
        return self

    def run_(self, _sender):  # noqa: N802  - Obj-C selector name.
        try:
            self._result = self._fn()
        except BaseException as e:  # noqa: BLE001
            self._error = e


def _run_on_main_and_wait(fn):
    """Run ``fn()`` on the main thread, block until it returns, and return its value.

    Exceptions raised by ``fn`` are re-raised on the caller's thread.
    """
    runner = _MainThreadRunner.alloc().initWithCallable_(fn)
    runner.performSelectorOnMainThread_withObject_waitUntilDone_(
        "run:", None, True
    )
    if runner._error is not None:
        raise runner._error
    return runner._result


# --- native two-field credentials dialog -----------------------------------


def _native_credentials_dialog(
    *,
    current_email: str = "",
) -> tuple[bool, str, str]:
    """Show a single NSAlert with email + password fields.

    Returns ``(clicked_ok, email, password)``. Must run on the main
    thread — dispatch via :func:`_run_on_main_and_wait` from worker
    threads.
    """
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Update LibreLinkUp Credentials")
    alert.setInformativeText_(
        "Email and password are stored in the macOS Keychain, not on disk."
    )
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")

    # 300×22 is the standard NSTextField height macOS uses in alert
    # accessory views; anything smaller clips the baseline of the caret.
    row_width, row_height = 300, 22
    email_field = NSTextField.alloc().initWithFrame_(
        NSMakeRect(0, 0, row_width, row_height)
    )
    email_field.setPlaceholderString_("email@example.com")
    email_field.setStringValue_(current_email)
    password_field = NSSecureTextField.alloc().initWithFrame_(
        NSMakeRect(0, 0, row_width, row_height)
    )
    password_field.setPlaceholderString_("password")

    stack = NSStackView.alloc().initWithFrame_(
        NSMakeRect(0, 0, row_width, row_height * 2 + 8)
    )
    stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
    stack.setSpacing_(8.0)
    stack.addArrangedSubview_(email_field)
    stack.addArrangedSubview_(password_field)
    # Pin each field to the stack's full width so they don't collapse
    # to their intrinsic zero-width content size.
    email_field.setTranslatesAutoresizingMaskIntoConstraints_(False)
    password_field.setTranslatesAutoresizingMaskIntoConstraints_(False)
    email_field.widthAnchor().constraintEqualToConstant_(row_width).setActive_(True)
    password_field.widthAnchor().constraintEqualToConstant_(row_width).setActive_(True)

    alert.setAccessoryView_(stack)
    # Focus the empty field so typing starts in the right place.
    alert.window().setInitialFirstResponder_(
        password_field if current_email else email_field
    )

    try:
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass

    response = alert.runModal()
    ok = response == NSAlertFirstButtonReturn
    return (ok, email_field.stringValue() or "", password_field.stringValue() or "")


def _native_single_field_dialog(
    *,
    title: str,
    message: str,
    default_text: str = "",
    ok_label: str = "OK",
    cancel_label: str = "Cancel",
) -> tuple[bool, str]:
    """Show a native NSAlert with a single text field. Main-thread only."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok_label)
    alert.addButtonWithTitle_(cancel_label)

    row_width, row_height = 300, 22
    field = NSTextField.alloc().initWithFrame_(
        NSMakeRect(0, 0, row_width, row_height)
    )
    field.setStringValue_(default_text)
    alert.setAccessoryView_(field)
    alert.window().setInitialFirstResponder_(field)

    try:
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass

    response = alert.runModal()
    ok = response == NSAlertFirstButtonReturn
    return (ok, field.stringValue() or "")


# Menubar title for each non-``ok`` daemon status. When status == "ok"
# (or "stale" with a last-known value) we render the number directly.
#
# The goal is a title that is readable at a glance without opening the
# dropdown — short but not cryptic. We keep the ``⚠`` prefix for any
# state that demands user attention, so the eye can spot "something's
# wrong" even without reading the words.
_STATUS_GLYPH = {
    "starting": "Starting…",
    "stale": "Stale",  # only used when the stale reading has no last value
    "warming_up": "Warming Up",
    "sensor_ended": "⚠ Sensor Ended",
    "no_network": "⚠ Offline",
    "auth_failed": "⚠ Sign In",
    "tos_required": "⚠ Accept Terms",
    "rate_limited": "⚠ Rate Limited",
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
        self._zone_pct_item = rumps.MenuItem(f"Time in Zones ({_GRAPH_HOURS}h): —")
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

        # Graph item at the top of the dropdown. ``rumps.MenuItem`` wraps an
        # ``NSMenuItem`` (exposed as ``._menuitem``); attaching a custom NSView
        # via ``setView_`` is the supported way to embed non-text content in
        # an AppKit menu. The empty title is ignored once a view is present.
        self._graph_view = GlucoseGraphView.alloc().initWithFrame_hours_(
            NSMakeRect(0, 0, _GRAPH_WIDTH, _GRAPH_HEIGHT), _GRAPH_HOURS
        )
        self._graph_view.setThresholds_(self._graph_thresholds())
        self._graph_item = rumps.MenuItem("")
        self._graph_item._menuitem.setView_(self._graph_view)

        self.menu = [
            self._graph_item,
            None,
            self._reading_updated_item,
            self._reading_unit_item,
            self._zone_pct_item,
            None,
            self._refresh_item,
            self._reauth_item,
            None,
            self._notifs_item,
            self._creds_item,
            self._thresholds_item,
        ]

        self._client = DaemonClient(base_url=base_url)

        # Explicit Timer rather than @rumps.timer — the decorator hides
        # the interval-mutation trick behind rumps internals; an explicit
        # Timer is easier to reason about (and to swap out in tests).
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
            self._set_title("⚠ Daemon Offline")
            self._reading_updated_item.title = "Background Service Offline"
            self._reading_unit_item.title = "Start the Daemon"
            self._zone_pct_item.title = f"Time in Zones ({_GRAPH_HOURS}h): —"
            self._graph_view.setPoints_([])
            return
        except DaemonError as e:
            log.warning("Daemon error: %s", e)
            self._set_title("⚠ Daemon Error")
            self._reading_updated_item.title = "Background Service Error"
            self._reading_unit_item.title = "Try Force Re-Authenticate"
            self._zone_pct_item.title = f"Time in Zones ({_GRAPH_HOURS}h): —"
            self._graph_view.setPoints_([])
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

        self._refresh_graph()

    def _refresh_graph(self) -> None:
        """Pull the latest cached history from the daemon and push it to the view.

        Errors here are non-fatal — the title and text rows already convey
        connectivity state, so a missing graph just clears to its empty
        placeholder rather than throwing up another error glyph.
        """
        try:
            points = self._client.graph(hours=_GRAPH_HOURS)
        except (DaemonUnreachable, DaemonError) as e:
            log.debug("Graph fetch failed: %s", e)
            self._graph_view.setPoints_([])
            self._zone_pct_item.title = f"Time in Zones ({_GRAPH_HOURS}h): —"
            return
        self._graph_view.setPoints_(
            [(p.timestamp.timestamp(), p.value_mg_dl) for p in points]
        )
        self._zone_pct_item.title = self._zone_percentages_line(points)

    def _graph_thresholds(self) -> GraphThresholds:
        return GraphThresholds(
            low=self.thresholds.low,
            high=self.thresholds.high,
            very_high=self.thresholds.very_high,
        )

    def _zone_percentages_line(self, points: list[GraphPoint]) -> str:
        """Return one-line time-in-zone summary for the current graph window."""
        prefix = f"Time in Zones ({_GRAPH_HOURS}h): "
        if len(points) < 2:
            return prefix + "—"
        points = sorted(points, key=lambda p: p.timestamp)
        seconds_by_zone = {"L": 0.0, "G": 0.0, "Y": 0.0, "O": 0.0}
        for a, b in zip(points, points[1:]):
            dt = (b.timestamp - a.timestamp).total_seconds()
            if dt <= 0:
                continue
            zone = self._zone_key(a.value_mg_dl)
            seconds_by_zone[zone] += dt
        total = sum(seconds_by_zone.values())
        if total <= 0:
            return prefix + "—"
        low_pct = int(round((seconds_by_zone["L"] / total) * 100))
        green_pct = int(round((seconds_by_zone["G"] / total) * 100))
        yellow_pct = int(round((seconds_by_zone["Y"] / total) * 100))
        orange_pct = int(round((seconds_by_zone["O"] / total) * 100))
        return (
            prefix
            f"L {low_pct}% | G {green_pct}% | "
            f"Y {yellow_pct}% | O {orange_pct}%"
        )

    def _zone_key(self, value_mg_dl: float) -> str:
        if value_mg_dl < self.thresholds.low:
            return "L"
        if value_mg_dl > self.thresholds.very_high:
            return "O"
        if value_mg_dl > self.thresholds.high:
            return "Y"
        return "G"

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
                lambda: self._show_alert(
                    title="Notifications Unavailable",
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
        """Capture email + password and save to Keychain.

        When running inside the ``Glucose Menubar.app`` wrapper bundle
        we show a single native NSAlert with two fields. When running
        unbundled (``python -m macos_frontend`` during development),
        AppKit modals can tear the process down, so we fall back to
        the osascript two-dialog flow which delegates UI to a properly
        bundled helper process.
        """
        if _is_bundled():
            ok, email, password = _run_on_main_and_wait(
                lambda: _native_credentials_dialog()
            )
        else:
            ok, email, password = self._osascript_credentials_prompt()
        if not ok:
            return

        email = email.strip()
        if not email:
            self._show_alert(
                title="Email Required",
                message="You didn't enter an email. Try Update Credentials… again.",
            )
            return
        if not password:
            self._show_alert(
                title="Password Required",
                message="You didn't enter a password. Try Update Credentials… again.",
            )
            return

        try:
            save_credentials(email=email, password=password)
        except Exception as e:  # noqa: BLE001
            self._show_alert(
                title="Could Not Save Credentials",
                message=f"{e}\n\nYou can still set LIBRELINKUP_EMAIL/_PASSWORD in .env.",
            )
            return
        self._show_alert(
            title="Credentials Saved",
            message=(
                f"Stored under Keychain service '{KEYCHAIN_SERVICE}'. "
                "Click 'Force Re-Authenticate' to use them now."
            ),
        )

    def _osascript_credentials_prompt(self) -> tuple[bool, str, str]:
        """Unbundled-mode fallback: two sequential osascript dialogs."""
        ok, email = _osascript_dialog(
            title="LibreLinkUp Email",
            message="Enter the email associated with your LibreLinkUp account.",
            ok="Next",
        )
        if not ok:
            return False, "", ""
        ok, password = _osascript_dialog(
            title="LibreLinkUp Password",
            message="Password is stored in the macOS Keychain, not on disk.",
            ok="Save",
            hidden_answer=True,
        )
        if not ok:
            return False, "", ""
        return True, email, password

    def _show_alert(self, *, title: str, message: str) -> None:
        """Show an informational alert, using whichever backend fits the mode."""
        if _is_bundled():
            def _impl() -> None:
                alert = NSAlert.alloc().init()
                alert.setMessageText_(title)
                alert.setInformativeText_(message)
                alert.addButtonWithTitle_("OK")
                try:
                    NSApp.activateIgnoringOtherApps_(True)
                except Exception:  # noqa: BLE001
                    pass
                alert.runModal()

            _run_on_main_and_wait(_impl)
        else:
            _osascript_alert(title=title, message=message)

    def _on_edit_thresholds(self, _sender) -> None:
        log.info("Set thresholds clicked")
        _run_in_background(self._edit_thresholds_impl)

    def _edit_thresholds_impl(self) -> None:
        """Prompt for the three colour thresholds. Session-only; see below."""
        default_text = (
            f"{int(self.thresholds.low)},"
            f"{int(self.thresholds.high)},"
            f"{int(self.thresholds.very_high)}"
        )
        message = (
            "Three numbers separated by commas: low, high, very_high.\n"
            f"Current: {int(self.thresholds.low)}, "
            f"{int(self.thresholds.high)}, "
            f"{int(self.thresholds.very_high)}.\n\n"
            "To persist across restarts, set THRESHOLD_LOW_MG_DL, "
            "THRESHOLD_HIGH_MG_DL, THRESHOLD_VERY_HIGH_MG_DL in .env."
        )

        if _is_bundled():
            ok, response = _run_on_main_and_wait(
                lambda: _native_single_field_dialog(
                    title="Glucose Thresholds (mg/dL)",
                    message=message,
                    default_text=default_text,
                    ok_label="Apply",
                )
            )
        else:
            ok, response = _osascript_dialog(
                title="Glucose Thresholds (mg/dL)",
                message=message,
                default_text=default_text,
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
            self._show_alert(title="Invalid Thresholds", message=str(e))
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
        # Push new band edges into the graph view immediately. ``setThresholds_``
        # only mutates a Python attribute and calls ``setNeedsDisplay:`` (which
        # AppKit documents as thread-safe); the actual redraw still happens on
        # the main thread on the next runloop pass.
        self._graph_view.setThresholds_(self._graph_thresholds())
        # The next rumps timer tick will re-render the title with the new
        # thresholds — we deliberately don't call ``_on_tick`` here because
        # that would touch AppKit UI off the main thread.


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
