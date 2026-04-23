"""macOS menubar frontend — a thin rumps wrapper around the daemon HTTP API.

This module knows nothing about LibreLinkUp, credentials, regions, or
trend codes. It maps daemon ``Status`` values to menubar glyphs and shows
the current value when the status allows it. That's the whole job.

Run::

    .venv/bin/python -m macos_frontend
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import objc
import rumps
from rumps import events as rumps_events
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
    NSMutableAttributedString,
    NSSecureTextField,
    NSStackView,
    NSTextField,
    NSUserInterfaceLayoutOrientationVertical,
)
from dotenv import load_dotenv
from Foundation import NSBundle, NSMinY, NSObject, NSPoint

# Allow ``python -m macos_frontend`` and ``python macos_frontend/app.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from glucose_common.credentials import (  # noqa: E402
    load_credentials,
    save_credentials,
)
from macos_frontend.api_client import (  # noqa: E402
    CurrentReading,
    DaemonClient,
    DaemonError,
    DaemonUnreachable,
)
from macos_frontend.graph_view import GlucoseGraphView, GraphThresholds  # noqa: E402

# Graph sits in the dropdown under the freshness and units text rows.
# 8 hours is the sweet spot between "see today's pattern" and "fits in a
# menu item without dominating the screen". Width is wide enough to make
# minute-resolution shape visible but not so wide that the menu overflows.
_GRAPH_HOURS = 8
_GRAPH_WIDTH = 320
_GRAPH_HEIGHT = 110
# Optional horizontal offset from the status button's leading edge (points); see
# ``_StatusItemMenuPresenter``.
_MENU_POPUP_X_NUDGE_PT = 0.0

# Title pulse animation. The value and arrow gently fade together on a
# shared cosine curve whenever the reading's trend is directional
# (rising or falling, slow or fast). Values chosen for a calm, ambient
# feel:
#   * ``_PULSE_PERIOD_S`` — 2s keeps it noticeable but still calm
#     (roughly ~30 cycles/min).
#   * ``_PULSE_MIN_ALPHA`` — keeps both the value and arrow clearly
#     legible at the dimmest point. Dropping below ~0.4 starts to read
#     as "broken"; pulling too close to 1.0 removes the effect.
#   * ``_PULSE_TICK_S`` — 10 fps; the glyphs are tiny and the motion is
#     low-amplitude, so anything faster burns CPU without improving
#     smoothness. Separate ``rumps.Timer`` so the poll cadence stays at
#     its configured value.
# Only codes 1/2/4/5 (↓ ↘ ↗ ↑) pulse. Code 3 (→ stable) is left static —
# animating a sideways arrow would suggest motion that isn't there.
# The trailing ``*`` on stale readings never pulses; it's a status flag,
# not part of the live reading.
_PULSE_PERIOD_S = 2.0
_PULSE_TICK_S = 0.1
_PULSE_MIN_ALPHA = 0.45
_ARROW_PULSE_SYMBOLS = frozenset({"↓", "↘", "↗", "↑"})

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
# ``NSAlert.runModal`` / ``rumps.Window.run`` behave similarly: the call
# appears to start, but AppKit silently marks the process as misbehaving
# and ``launchd`` tears it down a few seconds later. There's no crash report,
# just a clean exit and a relaunch — which from the user's perspective looks
# like "the menu went dead and nothing happened". In that mode we fall back to
# ``osascript``, which shells out to a properly bundled helper process
# (``System Events``) to host the dialog.
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


def _credentials_dialog_prefill() -> tuple[str, str]:
    """Best-known email and password for the account dialog (Keychain, then ``.env``).

    Password is only returned when it comes from the same source as the email
    (see :func:`load_credentials` — no mixing Keychain email with env password).
    """
    env_email = (os.environ.get("LIBRELINKUP_EMAIL") or "").strip() or None
    env_password = os.environ.get("LIBRELINKUP_PASSWORD") or None
    loaded = load_credentials(env_email, env_password)
    if loaded is not None:
        return loaded.email.strip(), loaded.password
    if env_email:
        return env_email, env_password or ""
    return "", ""


def _native_credentials_dialog(
    *,
    current_email: str = "",
    current_password: str = "",
) -> tuple[bool, str, str]:
    """Show a single NSAlert with email + password fields.

    Returns ``(clicked_ok, email, password)``. Must run on the main
    thread — dispatch via :func:`_run_on_main_and_wait` from worker
    threads.
    """
    alert = NSAlert.alloc().init()
    alert.setMessageText_("LibreLinkUp sign-in")
    alert.setInformativeText_(
        "Your email and password are stored securely in your Mac Keychain, not as a plain file."
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
    if current_password:
        password_field.setStringValue_(current_password)

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
    # Focus the first field that still needs attention.
    if not current_email.strip():
        first = email_field
    elif not current_password:
        first = password_field
    else:
        first = password_field
    alert.window().setInitialFirstResponder_(first)

    try:
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass

    response = alert.runModal()
    ok = response == NSAlertFirstButtonReturn
    return (ok, email_field.stringValue() or "", password_field.stringValue() or "")


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
    "rate_limited": "⚠ Slow Down",
}


def _title_parts(reading: CurrentReading) -> tuple[str, str, str]:
    """Split the title into (value, arrow, trailing) for animated rendering.

    The pulse animation needs to apply an alpha to the arrow glyph only,
    so the title can't be a single opaque string. We return three pieces:

    * ``value`` — the numeric reading (or a status glyph when there's no
      number to show). Always present.
    * ``arrow`` — the trend symbol (possibly empty). Includes a leading
      space so joining the pieces produces the same spacing as before.
    * ``trailing`` — status suffix; currently just ``"*"`` on stale
      readings to flag "this might be old".

    Error states carry the whole glyph in ``value`` with the other two
    empty — ``⚠ Offline`` shouldn't pulse anything.
    """
    if reading.status == "ok" and reading.value is not None:
        value = str(int(round(reading.value)))
        arrow = reading.trend_symbol or ""
        arrow_part = f" {arrow}" if arrow else ""
        return value, arrow_part, ""
    if reading.status == "stale" and reading.value is not None:
        value = str(int(round(reading.value)))
        arrow = reading.trend_symbol or ""
        arrow_part = f" {arrow}" if arrow else ""
        return value, arrow_part, "*"
    return _STATUS_GLYPH.get(reading.status, "⚠"), "", ""


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
            updated_line = f"Last updated {age} at {local_hhmm}"
        else:
            updated_line = f"Last updated {age}"
        unit_line = f"Unit: {unit}" if unit else "Unit: —"
        return (updated_line, unit_line)
    status = _STATUS_TEXT.get(reading.status, "Unavailable")
    detail = _DETAIL_TEXT.get(reading.status, "")
    if detail:
        return (status, detail)
    return (status, "—")


def _humanize_age(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "Unknown"
    if age_seconds < 0:
        # Clock skew or bad payload timestamp; avoid showing confusing negatives.
        return "Just now"
    if age_seconds < 60:
        return "Just now"
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
    "warming_up": "Sensor Warming Up",
    "sensor_ended": "Sensor Ended",
    "no_network": "No Internet Connection",
    "auth_failed": "Sign-In Required",
    "tos_required": "Open FreeStyle LibreLink to Accept Terms",
    "rate_limited": "Please Wait a Moment",
}

_DETAIL_TEXT = {
    "starting": "Getting First Reading",
    "warming_up": "Try Again in a Few Minutes",
    "sensor_ended": "Replace Sensor in FreeStyle LibreLink",
    "no_network": "Check Wi-Fi and Try Refresh Now",
    "auth_failed": "Choose “Refresh LibreLinkUp Session” below",
    "tos_required": "Finish any prompts in FreeStyle LibreLink",
    "rate_limited": "Retrying automatically",
}


class _StatusItemMenuPresenter(NSObject):
    """Pops up the main ``NSMenu`` with its leading edge on the pill's leading edge.

    rumps attaches the menu with ``NSStatusItem.setMenu:``, which anchors the
    panel to the trailing edge of the pill and looks visually disconnected from
    the number. We clear that menu, handle the button action, and call
    ``popUpMenuPositioningItem:atLocation:inView:`` with the menu's **leading**
    edge at the status button's **leading** edge (in button coordinates), plus
    ``_MENU_POPUP_X_NUDGE_PT``.
    """

    def initWithGlucoseApp_(self, app: "GlucoseApp"):
        self = objc.super(_StatusItemMenuPresenter, self).init()
        if self is None:
            return None
        self._glucose_app = app
        return self

    def showGlucoseMenu_(self, sender):
        app = self._glucose_app
        ns_menu = app._menu._menu
        button = sender
        ns_menu.sizeToFit()
        menu_w = float(ns_menu.size().width)
        x = _MENU_POPUP_X_NUDGE_PT
        bw = float(button.bounds().size.width)
        if menu_w <= bw:
            # Cap the right edge so the menu stays inside the pill when it is
            # narrower than the status item.
            x = min(x, bw - menu_w)
        loc = NSPoint(x, NSMinY(button.bounds()))
        ns_menu.popUpMenuPositioningItem_atLocation_inView_(None, loc, button)


class GlucoseApp(rumps.App):
    def __init__(
        self,
        base_url: str,
        poll_interval: int,
        thresholds: ColorThresholds,
    ):
        super().__init__("···", quit_button="Quit")
        self.poll_interval = poll_interval
        self.thresholds = thresholds

        self._reading_updated_item = rumps.MenuItem("Connecting…")
        self._units_item = rumps.MenuItem("—")
        self._refresh_item = rumps.MenuItem("Refresh Now", callback=self._on_refresh)
        self._reauth_item = rumps.MenuItem(
            "Refresh LibreLinkUp Session", callback=self._on_reauth
        )
        self._creds_item = rumps.MenuItem(
            "LibreLinkUp Account…", callback=self._on_edit_credentials
        )

        # Status lines first, then the graph — no separator between text and
        # graph so it reads as one block. ``rumps.MenuItem`` wraps an
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
            self._reading_updated_item,
            self._units_item,
            self._graph_item,
            None,
            self._refresh_item,
            self._reauth_item,
            None,
            self._creds_item,
        ]

        self._client = DaemonClient(base_url=base_url)

        # Cached pieces of the current title. Set by ``_on_tick`` and
        # re-read by ``_on_animation_tick`` between polls so the arrow
        # pulse can re-render the title without re-fetching from the
        # daemon on every animation frame.
        self._title_value: str = ""
        self._title_arrow: str = ""
        self._title_trailing: str = ""
        self._title_color = None
        # Wall-clock anchor for the pulse phase. Using a monotonic clock
        # means the animation keeps phase across daemon hiccups and
        # doesn't jump when the system clock is adjusted.
        self._arrow_pulse_anchor: float = time.monotonic()

        # Explicit Timer rather than @rumps.timer — the decorator hides
        # the interval-mutation trick behind rumps internals; an explicit
        # Timer is easier to reason about (and to swap out in tests).
        self._timer = rumps.Timer(self._on_tick, poll_interval)
        # Separate, faster timer for the trend-arrow pulse. Only started
        # when the current reading has a directional arrow; stopped as
        # soon as the trend goes stable (or the reading becomes an error
        # glyph) so we're not waking the runloop 10×/s for nothing.
        self._arrow_pulse_timer = rumps.Timer(
            self._on_arrow_pulse_tick, _PULSE_TICK_S
        )
        self._status_menu_presenter = _StatusItemMenuPresenter.alloc().initWithGlucoseApp_(self)
        rumps_events.before_start.register(self._install_number_aligned_status_menu)
        self._timer.start()
        self._on_tick(None)

    def _install_number_aligned_status_menu(self, *_unused) -> None:
        delegate = self._nsapp
        item = delegate.nsstatusitem
        item.setMenu_(None)
        button = item.button()
        if button is None:
            return
        button.setTarget_(self._status_menu_presenter)
        button.setAction_("showGlucoseMenu:")

    # --- title rendering ---

    def _set_title(self, text: str, color=None) -> None:
        """Set a plain (non-arrowed) menubar title.

        Used for error glyphs that have no trend arrow to animate. The
        ``ok``/``stale`` paths go through :meth:`_render_title_parts`
        instead so the value and arrow can fade under the pulse.
        """
        self._render_title_parts(
            text, "", "", color, value_alpha=1.0, arrow_alpha=1.0
        )

    def _render_title_parts(
        self,
        value_text: str,
        arrow_text: str,
        trailing_text: str,
        color,
        value_alpha: float,
        arrow_alpha: float,
    ) -> None:
        """Paint ``value + arrow + trailing`` into the status bar button.

        rumps's ``self.title = ...`` only handles plain text. To colour
        the text (and to fade individual runs under the pulse) we set
        ``NSStatusBarButton.attributedTitle`` with a mutable attributed
        string carrying per-range attributes. All title updates go
        through here so the two title channels never conflict — mixing
        plain and attributed titles leaves stale colour from a previous
        tick visible.

        The three runs are coloured independently:

        * ``value_text`` fades at ``value_alpha``.
        * ``arrow_text`` fades at ``arrow_alpha``.
        * ``trailing_text`` always paints at full alpha; its only
          current use is the ``*`` on stale readings, which is a status
          flag and shouldn't share the live-data pulse.

        Kept as two parameters (not one) because it lets callers fade
        runs independently if we ever want to, even though the current
        caller passes the same value to both.
        """
        full_text = f"{value_text}{arrow_text}{trailing_text}"

        nsapp = getattr(self, "_nsapp", None)
        if nsapp is None or not hasattr(nsapp, "nsstatusitem"):
            # Pre-runloop tick (very early in __init__) — fall back to plain.
            self.title = full_text
            return
        button = nsapp.nsstatusitem.button()
        if button is None:
            self.title = full_text
            return

        base_color = color if color is not None else NSColor.labelColor()
        # Cocoa expects specific attribute-name constants; generic string
        # keys are ignored, which leaves the text in default monochrome.
        base_attrs = {
            NSForegroundColorAttributeName: base_color,
            NSFontAttributeName: NSFont.menuBarFontOfSize_(0),
        }
        try:
            attr_string = NSMutableAttributedString.alloc().initWithString_attributes_(
                full_text, base_attrs
            )
            # Range math: the three runs are appended in order, so their
            # locations are the cumulative lengths. ``arrow_text`` carries
            # its own leading space (see ``_title_parts``) so there's no
            # "+1" fudge for the separator here.
            if value_text and 0.0 <= value_alpha < 1.0:
                value_color = base_color.colorWithAlphaComponent_(value_alpha)
                attr_string.addAttribute_value_range_(
                    NSForegroundColorAttributeName,
                    value_color,
                    (0, len(value_text)),
                )
            if arrow_text and 0.0 <= arrow_alpha < 1.0:
                arrow_color = base_color.colorWithAlphaComponent_(arrow_alpha)
                attr_string.addAttribute_value_range_(
                    NSForegroundColorAttributeName,
                    arrow_color,
                    (len(value_text), len(arrow_text)),
                )
            button.setAttributedTitle_(attr_string)
        except Exception:  # noqa: BLE001
            # Never crash UI updates over attributed-title quirks.
            self.title = full_text

    def _pulse_envelope(self) -> float:
        """Shared 0..1 envelope for the title pulse.

        Cosine easing over ``_PULSE_PERIOD_S`` gives equal-feeling dwell
        at the bright and dim ends — reads as a natural breathing pulse
        rather than a rigid blink. Value and arrow alphas are both
        interpolated off this single envelope so they stay phase-locked;
        independent sines would drift visibly and feel uncoordinated.
        """
        elapsed = time.monotonic() - self._arrow_pulse_anchor
        phase = (elapsed / _PULSE_PERIOD_S) * 2 * math.pi
        return 0.5 + 0.5 * math.cos(phase)  # 0..1

    def _repaint_title(self, *, animating: bool) -> None:
        """Render the cached title parts at the current animation phase."""
        if animating:
            env = self._pulse_envelope()
            alpha = _PULSE_MIN_ALPHA + (1.0 - _PULSE_MIN_ALPHA) * env
        else:
            alpha = 1.0
        # Value and arrow share one alpha so they pulse identically;
        # applying the same number to both ranges is simpler than two
        # parameters that always happen to be equal.
        self._render_title_parts(
            self._title_value,
            self._title_arrow,
            self._title_trailing,
            self._title_color,
            value_alpha=alpha,
            arrow_alpha=alpha,
        )

    def _on_arrow_pulse_tick(self, _sender) -> None:
        # Cheap safety net: if we somehow end up here with a non-directional
        # arrow, just stop the timer rather than burning frames.
        if self._title_arrow.strip() not in _ARROW_PULSE_SYMBOLS:
            self._arrow_pulse_timer.stop()
            self._repaint_title(animating=False)
            return
        self._repaint_title(animating=True)

    def _apply_title_parts(
        self,
        value_text: str,
        arrow_text: str,
        trailing_text: str,
        color,
    ) -> None:
        """Cache the title parts + manage the pulse timer, then paint once.

        Called by :meth:`_on_tick` on every poll. Whether the pulse
        animation runs is a function of the arrow glyph: directional
        arrows (``↓ ↘ ↗ ↑``) pulse; the sideways ``→`` and empty strings
        sit still. Changing the arrow resets the pulse phase so the
        transition starts at full brightness rather than snapping
        mid-cycle, which would read as a glitch.
        """
        animate = arrow_text.strip() in _ARROW_PULSE_SYMBOLS
        # Reset the phase when the displayed arrow changes so a new
        # trend enters the screen at peak alpha rather than mid-fade.
        if arrow_text != self._title_arrow:
            self._arrow_pulse_anchor = time.monotonic()

        self._title_value = value_text
        self._title_arrow = arrow_text
        self._title_trailing = trailing_text
        self._title_color = color

        if animate:
            if not self._arrow_pulse_timer.is_alive():
                self._arrow_pulse_timer.start()
        else:
            if self._arrow_pulse_timer.is_alive():
                self._arrow_pulse_timer.stop()

        self._repaint_title(animating=animate)

    # --- timer / menu callbacks ---

    def _on_tick(self, _sender) -> None:
        try:
            reading = self._client.current()
        except DaemonUnreachable as e:
            log.warning("Daemon unreachable: %s", e)
            self._apply_title_parts("⚠ Not Connected", "", "", color=None)
            self._reading_updated_item.title = "Can't reach FreeStyle LibreLink"
            self._units_item.title = "—"
            self._graph_view.setPoints_([])
            return
        except DaemonError as e:
            log.warning("Daemon error: %s", e)
            self._apply_title_parts("⚠ Error", "", "", color=None)
            self._reading_updated_item.title = "Couldn't load your reading"
            self._units_item.title = "—"
            self._graph_view.setPoints_([])
            return

        value_part, arrow_part, trailing_part = _title_parts(reading)
        # Only colour when we actually have a numeric value to evaluate.
        # Stale readings still get coloured (the value is the most recent
        # known truth), error glyphs use default text colour.
        color = None
        value_for_color = reading.value_in_mg_per_dl
        if reading.status in ("ok", "stale") and value_for_color is not None:
            color = _color_for(value_for_color, self.thresholds)
        self._apply_title_parts(value_part, arrow_part, trailing_part, color=color)

        u_line, units_line = _reading_menu_labels(reading)
        self._reading_updated_item.title = u_line
        self._units_item.title = units_line

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
            return
        self._graph_view.setPoints_(
            [(p.timestamp.timestamp(), p.value_mg_dl) for p in points]
        )

    def _graph_thresholds(self) -> GraphThresholds:
        return GraphThresholds(
            low=self.thresholds.low,
            high=self.thresholds.high,
            very_high=self.thresholds.very_high,
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
        pref_email, pref_password = _credentials_dialog_prefill()
        if _is_bundled():
            ok, email, password = _run_on_main_and_wait(
                lambda: _native_credentials_dialog(
                    current_email=pref_email,
                    current_password=pref_password,
                )
            )
        else:
            ok, email, password = self._osascript_credentials_prompt(
                default_email=pref_email,
            )
        if not ok:
            return

        email = email.strip()
        if not email:
            self._show_alert(
                title="Email missing",
                message="Enter the email for your LibreLinkUp account, then open LibreLinkUp Account… again.",
            )
            return
        if not password:
            self._show_alert(
                title="Password missing",
                message="Enter your LibreLinkUp password, then open LibreLinkUp Account… again.",
            )
            return

        try:
            save_credentials(email=email, password=password)
        except Exception as e:  # noqa: BLE001
            self._show_alert(
                title="Couldn't save sign-in",
                message=(
                    f"{e}\n\n"
                    "If you're comfortable editing config files, you can set "
                    "LIBRELINKUP_EMAIL and LIBRELINKUP_PASSWORD in `.env` instead."
                ),
            )
            return
        self._show_alert(
            title="Sign-in saved",
            message=(
                "Saved to your Mac Keychain. "
                "Choose “Refresh LibreLinkUp Session” from the menu to connect with these details now."
            ),
        )

    def _osascript_credentials_prompt(
        self, *, default_email: str = ""
    ) -> tuple[bool, str, str]:
        """Unbundled-mode fallback: two sequential osascript dialogs."""
        ok, email = _osascript_dialog(
            title="LibreLinkUp email",
            message="Enter the email you use for LibreLinkUp.",
            default_text=default_email,
            ok="Next",
        )
        if not ok:
            return False, "", ""
        ok, password = _osascript_dialog(
            title="LibreLinkUp password",
            message="This is saved in your Mac Keychain, not as a plain file.",
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


def main() -> int:
    load_dotenv(_REPO_ROOT / ".env")
    base_url = os.environ.get("DAEMON_URL", "http://127.0.0.1:8765")
    poll_interval = int(os.environ.get("FRONTEND_POLL_SECONDS", "30"))
    thresholds = ColorThresholds.from_env()
    log.info(
        "Starting frontend (daemon=%s, poll=%ss, "
        "thresholds: <%g red, %g–%g green, %g–%g yellow, >%g orange)",
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
