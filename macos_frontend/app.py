"""macOS menubar frontend — a thin rumps wrapper around the daemon HTTP API.

This module maps daemon ``Status`` values to menubar glyphs and shows the
current value when the status allows it. It also owns the LibreView
credentials UX: on first launch (Keychain is empty) it prompts the user
and seeds the Keychain, and the **LibreView Account** menu item lets
them update the stored credentials later.

Run::

    scripts/install_launchagent.sh install
"""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import objc
import rumps
from AppKit import (
    NSAboutPanelOptionApplicationIcon,
    NSAboutPanelOptionApplicationName,
    NSAboutPanelOptionCredits,
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSEventModifierFlagCommand,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSLayoutConstraint,
    NSLinkAttributeName,
    NSMakeRect,
    NSMutableAttributedString,
    NSPanel,
    NSSecureTextField,
    NSTextField,
    NSView,
    NSWindowStyleMaskTitled,
)
from dotenv import load_dotenv
from Foundation import NSURL, NSBundle, NSMinY, NSObject, NSPoint
from rumps import events as rumps_events

# Keep repo root importable when launched from the app wrapper.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from glucose_common.credentials import (  # noqa: E402
    clear_credentials,
    load_credentials,
    save_credentials,
)
from macos_frontend.api_client import (  # noqa: E402
    CurrentReading,
    DaemonClient,
    DaemonError,
    DaemonNotReady,
    DaemonUnreachable,
)
from macos_frontend.graph_view import GlucoseGraphView, GraphThresholds  # noqa: E402

# Graph sits in the dropdown under the freshness text row.
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
_AUTH_RETRY_CHECK_TIMEOUT_S = 12.0
_AUTH_RETRY_CHECK_INTERVAL_S = 0.75
_FRONTEND_POLL_SECONDS = 30
_THRESHOLD_LOW_MG_DL = 70.0
_THRESHOLD_HIGH_MG_DL = 180.0
_THRESHOLD_VERY_HIGH_MG_DL = 250.0

_ABOUT_TAGLINE = (
    "GlucoBar keeps your latest LibreView glucose reading and trend visible in the macOS "
    "menu bar, so you can see whether you are in range, rising, or dropping while focused "
    "on your Mac. It helps you notice high or low blood sugar values sooner without having "
    "to pick up your phone to check."
)
_AUTHOR_NAME = "Eduardo Santos"
_REPO_URL = "https://github.com/eduardo/librelinkup-glucose-menubar-python"

# How long to wait after the AppKit runloop starts before popping the
# first-run credentials modal. Short enough that a fresh user never
# notices they saw the menubar first, long enough that the runloop
# has done one event pass so ``runModalForWindow_`` renders cleanly.
_FIRST_RUN_PROMPT_DELAY_S = 0.3
_STATE_DIR = Path(os.environ.get("DAEMON_STATE_DIR", str(Path.home() / ".glucobar"))).expanduser()
_KEYCHAIN_NOTICE_SENTINEL = _STATE_DIR / ".keychain_notice_seen"
_KEYCHAIN_NOTICE_TITLE = "About the macOS Keychain prompt"
_KEYCHAIN_NOTICE_MESSAGE = (
    "GlucoBar stores your LibreView sign-in in your Mac Keychain. "
    "macOS may show a system dialog asking if GlucoBar can access that item.\n\n"
    "Recommended: click \"Always Allow\" so you are not asked on every launch."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("glucose.frontend")

# Dialog strategy: native ``NSPanel`` modals only.
#
# When launched from launchd via the ``GlucoBar.app`` wrapper bundle
# (see ``scripts/app_bundle/``), AppKit sees a real
# ``CFBundleIdentifier`` and ``LSUIElement`` and modal dialogs work
# correctly — so we show a titled ``NSPanel`` with two fields for
# credentials (email + password in a single modal) and a matching
# single-OK ``NSPanel`` for informational alerts. We deliberately avoid
# ``NSAlert`` here: it always reserves space for the app icon and
# duplicates headings we already draw inside the content. ``NSPanel``
# keeps credentials and info dialogs visually aligned.
#
# Dialog flow runs on a background thread so the
# rumps menu callback returns immediately (otherwise the menu stays
# wedged in its tracking session). We use
# ``performSelectorOnMainThread:`` to marshal the actual UI call back
# to the main thread.


def _about_credits_attributed_string() -> NSMutableAttributedString:
    """Credits text for the system About panel (includes tappable repo link)."""
    body = (
        f"{_ABOUT_TAGLINE}\n\n"
        f"Created by\n{_AUTHOR_NAME}\n\n"
        "Repository\n"
        f"{_REPO_URL}\n\n"
        "License\n"
        "Apache License 2.0. See the LICENSE and NOTICE files in the source repository.\n"
        "The software is provided as-is, without warranties or guarantees.\n\n"
        "Disclaimer\n"
        "GlucoBar is not a medical device and must not be used for diagnosis, treatment, "
        "dosing, emergency decisions, or any other medical decision. Readings may be delayed, "
        "missing, inaccurate, or unavailable. Always verify glucose information with your "
        "official LibreView/LibreLink app, glucose meter, or qualified healthcare professional.\n\n"
        "This is an independent open-source project. It is not affiliated with, endorsed by, "
        "or supported by Abbott or LibreView. You use GlucoBar at your own risk."
    )
    attr = NSMutableAttributedString.alloc().initWithString_(body)
    start = body.find(_REPO_URL)
    if start >= 0:
        attr.addAttribute_value_range_(
            NSLinkAttributeName,
            NSURL.URLWithString_(_REPO_URL),
            (start, len(_REPO_URL)),
        )
    return attr


def _about_icon_image():
    """Load the bundled app icon explicitly for the standard About panel."""
    path = NSBundle.mainBundle().pathForResource_ofType_("GlucoBar", "icns")
    if not path:
        return None
    return NSImage.alloc().initWithContentsOfFile_(path)


def _show_about_panel() -> None:
    """Open the standard About window with the bundled icon and credits."""
    opts = {
        NSAboutPanelOptionApplicationName: "GlucoBar",
        NSAboutPanelOptionCredits: _about_credits_attributed_string(),
    }
    icon = _about_icon_image()
    if icon is not None:
        opts[NSAboutPanelOptionApplicationIcon] = icon
    NSApp.orderFrontStandardAboutPanelWithOptions_(opts)
    try:
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass


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
# Any launch outside ``GlucoBar.app`` is unsupported and rejected at
# startup. We accept two "bundled enough" signals:
#   1) main bundle id matches ``com.glucobar.frontend`` (py2app build)
#   2) ``sys.executable`` path is inside ``GlucoBar.app/Contents/MacOS``
#      (source-wrapper bundle used by install_launchagent.sh).

_BUNDLE_ID = "com.glucobar.frontend"


def _is_bundled() -> bool:
    try:
        bid = NSBundle.mainBundle().bundleIdentifier()
    except Exception:  # noqa: BLE001
        bid = None
    if bid == _BUNDLE_ID:
        return True
    exe = Path(sys.executable)
    return (
        exe.name == "GlucoBar"
        and "GlucoBar.app" in exe.parts
        and "Contents" in exe.parts
        and "MacOS" in exe.parts
    )


def _ensure_bundled_launch() -> None:
    """Fail fast when launched outside ``GlucoBar.app``.

    Native AppKit modal flows require being launched from the app wrapper
    (or a fully packaged bundle); running this module directly is
    intentionally unsupported.
    """
    if _is_bundled():
        return
    raise RuntimeError(
        "Unsupported launch mode. Start GlucoBar from GlucoBar.app "
        "(e.g. run scripts/install_launchagent.sh install)."
    )


def _hide_dock_icon_by_default() -> None:
    """Prefer menu-bar-only activation policy.

    Bundled launches already have ``LSUIElement=true`` in ``Info.plist``,
    but setting Accessory keeps behavior explicit: menu bar item only, no
    Dock entry by default.
    """
    try:
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:  # noqa: BLE001
        log.debug("Could not set accessory activation policy", exc_info=True)


# --- main-thread dispatch --------------------------------------------------
#
# AppKit UI calls (NSApp.runModalForWindow_, NSApp.activateIgnoringOtherApps_, etc.)
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

# Menu row title shown before the gray username suffix.
_SIGN_OUT_MENU_PRIMARY_TITLE = "Sign Out of LibreView Account"
_SIGN_IN_MENU_PRIMARY_TITLE = "Sign In to LibreView Account"

# Plain title used when the credentials dialog is opened without custom copy.
_LIBREVIEW_ACCOUNT_TITLE = "LibreView Account"
_SIGN_IN_DIALOG_BODY = (
    "Sign in with your LibreView account so GlucoBar can read your glucose "
    "readings. Your email and password are stored securely in your Mac "
    "Keychain and never sent anywhere else."
)
_SIGN_IN_DIALOG_BUTTON = "Sign In"

# launchd label + plist path for the one and only LaunchAgent GlucoBar
# installs: the frontend, which in turn supervises the daemon as a
# child process via :mod:`macos_frontend.daemon_supervisor`. Used by
# the "Open at Login" menu item's launchctl enable/disable
# calls.
_FRONTEND_LAUNCHD_LABEL = "com.glucobar.frontend"
_FRONTEND_STARTUP_TITLE = "Open at Login"
_FRONTEND_PLIST_PATH = Path.home() / "Library/LaunchAgents/com.glucobar.frontend.plist"


def _credentials_dialog_prefill() -> tuple[str, str]:
    """Email + password currently in the Keychain, or empty strings if none."""
    loaded = load_credentials()
    if loaded is not None:
        return loaded.email.strip(), loaded.password
    return "", ""


def _has_stored_credentials() -> bool:
    """Return True iff both LibreView credentials are present in Keychain."""
    return load_credentials() is not None


def _kickstart_daemon() -> None:
    """Ask the daemon to restart so it picks up new credentials.

    Delegates to :func:`macos_frontend.daemon_supervisor.request_restart`,
    which only nudges the active in-process managed supervisor. In
    external mode this is best-effort and intentionally becomes a no-op:
    the daemon picks up new state on its next natural restart.
    """
    from macos_frontend.daemon_supervisor import request_restart
    request_restart()


def _push_credentials_to_supervisor(email: str, password: str) -> None:
    """Hand saved credentials to the supervisor so the daemon never reads Keychain."""
    from macos_frontend.daemon_supervisor import get_active

    sup = get_active()
    if sup is None:
        return
    sup.set_credentials(email, password)


def _clear_credentials_on_supervisor() -> None:
    """Forget supervisor-injected credentials, e.g. after Sign Out."""
    from macos_frontend.daemon_supervisor import get_active

    sup = get_active()
    if sup is None:
        return
    sup.clear_credentials()


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run launchctl with stdout/stderr captured."""
    return subprocess.run(
        ["/bin/launchctl", *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


def _frontend_startup_enabled() -> bool:
    """Return whether launchd startup is enabled for the frontend."""
    if not _FRONTEND_PLIST_PATH.exists():
        return False

    uid = os.getuid()
    disabled_target = f"gui/{uid}"
    try:
        disabled_proc = _launchctl("print-disabled", disabled_target)
    except Exception:  # noqa: BLE001
        log.exception("launchctl print-disabled failed")
        return False

    if disabled_proc.returncode == 0:
        # launchctl output shape: "... com.glucobar.frontend => true/false".
        disabled_match = re.search(
            rf"{re.escape(_FRONTEND_LAUNCHD_LABEL)}\s*=>\s*(true|false)",
            disabled_proc.stdout or "",
        )
        if disabled_match is not None:
            return disabled_match.group(1) == "false"

    # Fallback: treat a loaded label as enabled.
    print_target = f"{disabled_target}/{_FRONTEND_LAUNCHD_LABEL}"
    try:
        return _launchctl("print", print_target).returncode == 0
    except Exception:  # noqa: BLE001
        log.exception("launchctl print failed for %s", print_target)
        return False


class _CredentialsDialogController(NSObject):
    """Routes Save/Cancel button clicks to ``NSApp.stopModalWithCode_``.

    NSButton targets must be Obj-C objects with selector methods, so we
    can't just bind the buttons to plain Python callables.
    """

    def save_(self, _sender):  # noqa: N802  - Obj-C selector name.
        NSApp.stopModalWithCode_(1)

    def cancel_(self, _sender):  # noqa: N802  - Obj-C selector name.
        NSApp.stopModalWithCode_(0)


class _PasteFriendlySecureTextField(NSSecureTextField):
    """NSSecureTextField that explicitly handles Cmd+V in modal panels.

    In our custom NSPanel modal, some launches don't route the standard
    paste key equivalent through the normal responder chain for secure
    fields. Handling Cmd+V directly keeps password pasting reliable.
    """

    def performKeyEquivalent_(self, event):  # noqa: N802 - Obj-C selector name.
        try:
            is_cmd = bool(int(event.modifierFlags()) & int(NSEventModifierFlagCommand))
            key = (event.charactersIgnoringModifiers() or "").lower()
        except Exception:  # noqa: BLE001
            return objc.super(_PasteFriendlySecureTextField, self).performKeyEquivalent_(event)

        if is_cmd and key == "v":
            return bool(NSApp.sendAction_to_from_("paste:", None, self))
        return objc.super(_PasteFriendlySecureTextField, self).performKeyEquivalent_(event)


def _native_credentials_dialog(
    *,
    current_email: str = "",
    current_password: str = "",
    title: str = _LIBREVIEW_ACCOUNT_TITLE,
    body_text: str = (
        "Your email and password are stored securely in your Mac Keychain."
    ),
    save_button: str = "Save",
) -> tuple[bool, str, str]:
    """Show a titled modal panel with email + password fields.

    We use a plain ``NSPanel`` rather than ``NSAlert`` because NSAlert
    reserves a fixed slot for the app icon that can't be suppressed —
    a blank ``NSImage`` leaves an empty square where the icon would go.
    Returns ``(clicked_ok, email, password)``. Must run on the main
    thread — dispatch via :func:`_run_on_main_and_wait` from worker
    threads.

    ``title`` / ``body_text`` / ``save_button`` default to the
    basic "LibreView Account" framing. Callers can override these
    strings when they want onboarding-focused copy.
    """
    field_width = 320
    h_padding = 20
    v_padding = 20
    # 22 is the standard NSTextField height in system alerts; anything
    # smaller clips the baseline of the caret.
    row_height = 22

    body = NSTextField.wrappingLabelWithString_(body_text)
    body.setFont_(NSFont.systemFontOfSize_(NSFont.systemFontSize()))
    body.setTranslatesAutoresizingMaskIntoConstraints_(False)

    email_field = NSTextField.alloc().initWithFrame_(
        NSMakeRect(0, 0, field_width, row_height)
    )
    email_field.setPlaceholderString_("email@example.com")
    email_field.setStringValue_(current_email)
    email_field.setTranslatesAutoresizingMaskIntoConstraints_(False)

    password_field = _PasteFriendlySecureTextField.alloc().initWithFrame_(
        NSMakeRect(0, 0, field_width, row_height)
    )
    password_field.setPlaceholderString_("password")
    if current_password:
        password_field.setStringValue_(current_password)
    password_field.setTranslatesAutoresizingMaskIntoConstraints_(False)

    controller = _CredentialsDialogController.alloc().init()
    save_btn = NSButton.buttonWithTitle_target_action_(
        save_button, controller, "save:"
    )
    # "\r" gives the button the system default-blue style and wires
    # Return to trigger it; "\x1b" wires Cancel to the Escape key.
    save_btn.setKeyEquivalent_("\r")
    save_btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
    cancel_btn = NSButton.buttonWithTitle_target_action_(
        "Cancel", controller, "cancel:"
    )
    cancel_btn.setKeyEquivalent_("\x1b")
    cancel_btn.setTranslatesAutoresizingMaskIntoConstraints_(False)

    content = NSView.alloc().init()
    content.addSubview_(body)
    content.addSubview_(email_field)
    content.addSubview_(password_field)
    content.addSubview_(cancel_btn)
    content.addSubview_(save_btn)

    NSLayoutConstraint.activateConstraints_([
        body.leadingAnchor().constraintEqualToAnchor_constant_(
            content.leadingAnchor(), h_padding
        ),
        body.trailingAnchor().constraintEqualToAnchor_constant_(
            content.trailingAnchor(), -h_padding
        ),
        body.topAnchor().constraintEqualToAnchor_constant_(
            content.topAnchor(), v_padding
        ),
        email_field.leadingAnchor().constraintEqualToAnchor_(body.leadingAnchor()),
        email_field.trailingAnchor().constraintEqualToAnchor_(body.trailingAnchor()),
        email_field.topAnchor().constraintEqualToAnchor_constant_(
            body.bottomAnchor(), 14
        ),
        password_field.leadingAnchor().constraintEqualToAnchor_(body.leadingAnchor()),
        password_field.trailingAnchor().constraintEqualToAnchor_(body.trailingAnchor()),
        password_field.topAnchor().constraintEqualToAnchor_constant_(
            email_field.bottomAnchor(), 8
        ),
        save_btn.trailingAnchor().constraintEqualToAnchor_(body.trailingAnchor()),
        save_btn.topAnchor().constraintEqualToAnchor_constant_(
            password_field.bottomAnchor(), 16
        ),
        save_btn.bottomAnchor().constraintEqualToAnchor_constant_(
            content.bottomAnchor(), -v_padding
        ),
        cancel_btn.trailingAnchor().constraintEqualToAnchor_constant_(
            save_btn.leadingAnchor(), -8
        ),
        cancel_btn.centerYAnchor().constraintEqualToAnchor_(save_btn.centerYAnchor()),
    ])

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, field_width + 2 * h_padding, 180),
        NSWindowStyleMaskTitled,
        NSBackingStoreBuffered,
        False,
    )
    panel.setTitle_(title)
    panel.setContentView_(content)
    # Default behaviour closes the window on Cmd-W and releases it, which
    # would dangle a freed pointer across subsequent runModal cycles.
    panel.setReleasedWhenClosed_(False)
    # Focus the first field that still needs attention.
    if not current_email.strip():
        panel.setInitialFirstResponder_(email_field)
    else:
        panel.setInitialFirstResponder_(password_field)
    panel.layoutIfNeeded()
    panel.center()

    try:
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass

    panel.makeKeyAndOrderFront_(None)
    try:
        code = NSApp.runModalForWindow_(panel)
    finally:
        panel.orderOut_(None)
    ok = int(code) == 1
    return (ok, email_field.stringValue() or "", password_field.stringValue() or "")


class _InfoAlertController(NSObject):
    """Routes the single OK button back to ``NSApp.stopModal``."""

    def ok_(self, _sender):  # noqa: N802 — Obj-C selector name.
        NSApp.stopModalWithCode_(1)


def _native_info_alert(*, title: str, message: str) -> None:
    """Show a single-OK informational modal with no icon slot.

    ``NSAlert`` always reserves horizontal space for the app icon and
    paints the bundle's ``NSApplicationIcon``. That looks
    off-brand on success/error messages, so we use an ``NSPanel``
    here — the same trick the credentials dialog already uses to
    dodge the icon slot. A bold title sits above the wrapping body,
    and a single OK button doubles as Return/Escape dismiss.

    Must run on the main thread; dispatch from a worker via
    :func:`_run_on_main_and_wait`.
    """
    content_width = 320
    h_padding = 20
    v_padding = 20

    title_label = NSTextField.wrappingLabelWithString_(title)
    title_label.setFont_(NSFont.boldSystemFontOfSize_(NSFont.systemFontSize()))
    title_label.setTranslatesAutoresizingMaskIntoConstraints_(False)

    body = NSTextField.wrappingLabelWithString_(message)
    body.setFont_(NSFont.systemFontOfSize_(NSFont.systemFontSize()))
    body.setTranslatesAutoresizingMaskIntoConstraints_(False)

    controller = _InfoAlertController.alloc().init()
    ok_btn = NSButton.buttonWithTitle_target_action_("OK", controller, "ok:")
    # "\r" makes Return activate the button (also gives it the blue
    # default-button styling); "\x1b" wires Escape to the same action
    # so single-button alerts can be dismissed with either key.
    ok_btn.setKeyEquivalent_("\r")
    ok_btn.setTranslatesAutoresizingMaskIntoConstraints_(False)

    content = NSView.alloc().init()
    content.addSubview_(title_label)
    content.addSubview_(body)
    content.addSubview_(ok_btn)

    NSLayoutConstraint.activateConstraints_([
        title_label.leadingAnchor().constraintEqualToAnchor_constant_(
            content.leadingAnchor(), h_padding
        ),
        title_label.trailingAnchor().constraintEqualToAnchor_constant_(
            content.trailingAnchor(), -h_padding
        ),
        title_label.topAnchor().constraintEqualToAnchor_constant_(
            content.topAnchor(), v_padding
        ),
        body.leadingAnchor().constraintEqualToAnchor_(title_label.leadingAnchor()),
        body.trailingAnchor().constraintEqualToAnchor_(title_label.trailingAnchor()),
        body.topAnchor().constraintEqualToAnchor_constant_(
            title_label.bottomAnchor(), 8
        ),
        ok_btn.trailingAnchor().constraintEqualToAnchor_(title_label.trailingAnchor()),
        ok_btn.topAnchor().constraintEqualToAnchor_constant_(
            body.bottomAnchor(), 16
        ),
        ok_btn.bottomAnchor().constraintEqualToAnchor_constant_(
            content.bottomAnchor(), -v_padding
        ),
    ])

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, content_width + 2 * h_padding, 160),
        NSWindowStyleMaskTitled,
        NSBackingStoreBuffered,
        False,
    )
    # Empty string rather than ``title`` — the bold label inside the
    # panel already carries the heading; duplicating it in the window
    # chrome just reads as noise.
    panel.setTitle_("")
    panel.setContentView_(content)
    panel.setReleasedWhenClosed_(False)
    panel.setInitialFirstResponder_(ok_btn)
    panel.layoutIfNeeded()
    panel.center()

    try:
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass

    panel.makeKeyAndOrderFront_(None)
    try:
        NSApp.runModalForWindow_(panel)
    finally:
        panel.orderOut_(None)


# Menubar title for each non-``ok`` daemon status. When status == "ok"
# (or "stale" with a last-known value) we render the number directly.
#
# The goal is a title that is readable at a glance without opening the
# dropdown — short but not cryptic, and tight enough that the user never
# has to open the menu to tell what's going on. Statuses are collapsed
# into a small handful of outcomes:
#
# * ``Connecting…`` — any transient "waiting for data" state (daemon
#   starting up, sensor still warming up, backend told us to slow down,
#   or a stale cache with no value yet). No ``⚠`` because there's nothing
#   for the user to do.
# * ``⚠ Offline`` — we can't reach the local daemon over HTTP (timeouts,
#   resets, etc.). A ``connection refused`` while its socket is still
#   down uses ``Connecting…`` instead, same family as HTTP 503 ``starting``.
# * ``⚠ Sign In`` — the user has to go fix something at the sign-in
#   layer (wrong credentials *or* an unaccepted Terms-of-Service prompt
#   in the FreeStyle LibreLink phone app). Both paths funnel the user
#   toward the same two menu items (``Refresh`` and
#   ``LibreView Account``) so distinguishing them in the title would
#   only add noise.
# * ``⚠ Error`` — unexpected/unhandled failures.
#
_STATUS_GLYPH = {
    "starting": "Connecting…",
    "stale": "Connecting…",  # only used when stale has no last value
    "warming_up": "Connecting…",
    "rate_limited": "Connecting…",
    "no_network": "⚠ Offline",
    "auth_failed": "⚠ Sign In",
    "tos_required": "⚠ Sign In",
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


def _color_for(value_mg_dl: float, t: ColorThresholds):
    """Return an ``NSColor`` for the menubar title, or ``None`` for default."""
    if value_mg_dl < t.low:
        return NSColor.systemRedColor()
    if value_mg_dl > t.very_high:
        return NSColor.systemOrangeColor()
    if value_mg_dl > t.high:
        return NSColor.systemYellowColor()
    return NSColor.systemGreenColor()


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


def _refresh_secondary_label(age_seconds: int) -> str:
    """Right-column freshness hint for the Refresh menu row.

    Under a minute we say ``Updated now`` (the API only produces a new
    sample roughly once a minute, so this covers the full dead zone);
    past that we hand off to :func:`_humanize_age` for ``X min ago`` /
    ``X hours ago``.
    """
    if age_seconds < 60:
        return "Updated now"
    return _humanize_age(age_seconds)


def _to_local_hhmm(timestamp_iso: str | None) -> str:
    if not timestamp_iso:
        return "—"
    try:
        # Daemon sends UTC with trailing Z.
        dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return "—"


def _parse_sensor_timestamp(timestamp_iso: str | None) -> datetime | None:
    """Parse the daemon's UTC sensor timestamp into an aware ``datetime``."""
    if not timestamp_iso:
        return None
    try:
        return datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


class _StatusItemMenuPresenter(NSObject):
    """Pops up the main ``NSMenu`` with its leading edge on the pill's leading edge.

    rumps attaches the menu with ``NSStatusItem.setMenu:``, which anchors the
    panel to the trailing edge of the pill and looks visually disconnected from
    the number. We clear that menu, handle the button action, and call
    ``popUpMenuPositioningItem:atLocation:inView:`` with the menu's **leading**
    edge at the status button's **leading** edge (in button coordinates), plus
    ``_MENU_POPUP_X_NUDGE_PT``.
    """

    def initWithGlucoseApp_(self, app: GlucoseApp):
        self = objc.super(_StatusItemMenuPresenter, self).init()
        if self is None:
            return None
        self._glucose_app = app
        return self

    def showGlucoseMenu_(self, sender):
        app = self._glucose_app
        # If the daemon was unreachable on the last poll, retry once when the
        # user opens the menu so a slow-starting daemon is picked up without
        # waiting for the next periodic tick.
        if getattr(app, "_daemon_unreachable", False):
            app._on_tick(None)
        # Recompute the refresh row label and its enabled state
        # against the wall clock at open time, otherwise both would only
        # advance once per ``poll_interval`` and the row could read e.g.
        # "(Just now)" while disabled long after the reading went stale.
        app._update_refresh_item()
        app._update_startup_item()
        app._update_credentials_item()
        ns_menu = app._menu._menu
        button = sender
        ns_menu.sizeToFit()
        menu_w = float(ns_menu.size().width)
        # Stretch the graph row to the menu width so bands/line match separator
        # lines (the view was created at a fixed width before final sizeToFit).
        gv = app._graph_view
        if abs(float(gv.frame().size.width) - menu_w) > 0.5:
            gv.setFrame_(NSMakeRect(0, 0, menu_w, _GRAPH_HEIGHT))
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

        self._daemon_unreachable = False
        self._last_daemon_status: str = "starting"
        self._refresh_item = rumps.MenuItem("Refresh", callback=self._on_refresh)
        # Latest sensor timestamp (UTC datetime) used to recompute the
        # refresh row label live when the menu opens, instead
        # of relying on the daemon's pre-baked ``reading_age_seconds``
        # which only refreshes once per poll.
        self._last_sensor_dt: datetime | None = None
        self._creds_item = rumps.MenuItem(
            _SIGN_IN_MENU_PRIMARY_TITLE, callback=self._on_first_run_credentials
        )
        self._startup_item = rumps.MenuItem(
            _FRONTEND_STARTUP_TITLE, callback=self._on_toggle_startup
        )
        self._about_item = rumps.MenuItem("About GlucoBar", callback=self._on_about)

        # The menubar title carries the full status story (number, trend
        # arrow, or an error glyph), so the dropdown holds no text row —
        # Graph, then Refresh and account. ``rumps.MenuItem``
        # wraps an ``NSMenuItem`` (exposed as ``._menuitem``); attaching
        # a custom NSView via ``setView_`` is the supported way to embed
        # non-text content in an AppKit menu. The empty title is ignored
        # once a view is present.
        self._graph_view = GlucoseGraphView.alloc().initWithFrame_hours_(
            NSMakeRect(0, 0, _GRAPH_WIDTH, _GRAPH_HEIGHT), _GRAPH_HOURS
        )
        self._graph_view.setThresholds_(self._graph_thresholds())
        self._graph_item = rumps.MenuItem("")
        self._graph_item._menuitem.setView_(self._graph_view)

        self.menu = [
            self._graph_item,
            self._refresh_item,
            None,
            self._startup_item,
            self._creds_item,
            None,
            self._about_item,
        ]
        self._update_startup_item()

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

        # Startup bootstrap: before any Keychain read, show a one-time
        # explanatory modal so users know what the upcoming macOS prompt means.
        # Then continue into the normal credentials flow.
        self._startup_keychain_timer = rumps.Timer(
            self._on_startup_keychain_tick, _FIRST_RUN_PROMPT_DELAY_S
        )
        self._startup_keychain_timer.start()

        # First-run onboarding timer (armed only after startup bootstrap confirms
        # credentials are missing).
        self._first_run_timer: rumps.Timer | None = None

    def _maybe_show_keychain_notice(self) -> None:
        """Show a one-time explanation before the first Keychain access."""
        try:
            if _KEYCHAIN_NOTICE_SENTINEL.exists():
                return
            self._show_alert(
                title=_KEYCHAIN_NOTICE_TITLE,
                message=_KEYCHAIN_NOTICE_MESSAGE,
            )
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _KEYCHAIN_NOTICE_SENTINEL.write_text("shown\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.debug("Could not persist Keychain notice marker", exc_info=True)

    def _on_startup_keychain_tick(self, _timer) -> None:
        """Run one-shot startup Keychain bootstrap."""
        self._startup_keychain_timer.stop()
        self._maybe_show_keychain_notice()
        self._update_credentials_item()
        if load_credentials() is None:
            self._first_run_timer = rumps.Timer(
                self._on_first_run_tick, _FIRST_RUN_PROMPT_DELAY_S
            )
            self._first_run_timer.start()

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
        # Enforce a local logged-out state whenever the Keychain is empty,
        # even if an externally-managed daemon still has a live session.
        if not _has_stored_credentials():
            self._daemon_unreachable = False
            self._last_daemon_status = "logged_out"
            self._last_sensor_dt = None
            self._apply_title_parts("⚠ Sign In", "", "", color=None)
            self._graph_view.setPoints_([])
            self._update_refresh_item()
            self._update_credentials_item()
            return
        try:
            reading = self._client.current()
        except DaemonNotReady as e:
            # Race after login / ``install_launchagent``: menubar often
            # polls before the daemon's HTTP server is bound. Same glyph
            # family as HTTP 503 ``starting`` — not ``Offline``, which
            # reads like a WAN outage.
            log.warning("Daemon not accepting connections yet: %s", e)
            self._daemon_unreachable = True
            self._last_daemon_status = "starting"
            self._apply_title_parts("Connecting…", "", "", color=None)
            self._graph_view.setPoints_([])
            self._update_refresh_item()
            return
        except DaemonUnreachable as e:
            log.warning("Daemon unreachable: %s", e)
            self._daemon_unreachable = True
            self._last_daemon_status = "no_network"
            # Folded into the same label as ``no_network`` — from the
            # user's perspective it's the same failure ("we can't see
            # your data right now"), and keeping two near-synonymous
            # glyphs is exactly the clutter we just trimmed.
            self._apply_title_parts("⚠ Offline", "", "", color=None)
            self._graph_view.setPoints_([])
            self._update_refresh_item()
            return
        except DaemonError as e:
            log.warning("Daemon error: %s", e)
            self._daemon_unreachable = False
            self._last_daemon_status = "error"
            self._apply_title_parts("⚠ Error", "", "", color=None)
            self._graph_view.setPoints_([])
            self._update_refresh_item()
            return

        self._daemon_unreachable = False
        self._last_daemon_status = reading.status
        value_part, arrow_part, trailing_part = _title_parts(reading)
        # Only colour when we actually have a numeric value to evaluate.
        # Stale readings still get coloured (the value is the most recent
        # known truth), error glyphs use default text colour.
        color = None
        value_for_color = reading.value_in_mg_per_dl
        if reading.status in ("ok", "stale") and value_for_color is not None:
            color = _color_for(value_for_color, self.thresholds)
        self._apply_title_parts(value_part, arrow_part, trailing_part, color=color)

        self._last_sensor_dt = _parse_sensor_timestamp(reading.sensor_timestamp)
        self._update_refresh_item()
        self._refresh_graph()

    def _refresh_graph(self) -> None:
        """Pull the latest cached history from the daemon and push it to the view.

        Errors here are non-fatal — the title and text rows already convey
        connectivity state, so a missing graph just clears to its empty
        placeholder rather than throwing up another error glyph.
        """
        try:
            points = self._client.graph(hours=_GRAPH_HOURS)
        except (DaemonNotReady, DaemonUnreachable, DaemonError) as e:
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

    def _current_reading_age_seconds(self) -> int | None:
        """Live age of the last sensor reading, computed from now.

        Returns ``None`` when no sensor timestamp has been recorded yet
        (e.g. before the first successful poll, or after a daemon error
        cleared the cached value).
        """
        if self._last_sensor_dt is None:
            return None
        delta = datetime.now(UTC) - self._last_sensor_dt
        return int(delta.total_seconds())

    def _update_refresh_item(self) -> None:
        """Sync the refresh row's label to the live reading age.

        The row always reads ``Refresh`` on the left and stays enabled;
        when we have a reading, a dim parenthesised freshness hint
        (``Updated now`` under a minute, then ``X min ago`` / ``X hours
        ago``) is appended in ``secondaryLabelColor`` so it reads as a
        visual aside rather than competing with the primary action.
        """
        age = self._current_reading_age_seconds()
        menu_item = self._refresh_item._menuitem
        if age is None:
            menu_item.setAttributedTitle_(None)
            self._refresh_item.title = "Refresh"
        else:
            self._apply_refresh_attr_title(_refresh_secondary_label(age))
        if self._refresh_item.callback is not self._on_refresh:
            self._refresh_item.set_callback(self._on_refresh)

    def _apply_refresh_attr_title(self, secondary: str) -> None:
        """Render ``Refresh (secondary)`` with the parenthetical dimmed.

        Keeping both halves on one line (rather than right-aligning via
        a tab stop) sidesteps the menu's variable content width — the
        freshness hint follows ``Refresh`` at a predictable distance
        regardless of how wide the graph is laid out.
        """
        primary = "Refresh"
        joiner = " ("
        closer = ")"
        full = f"{primary}{joiner}{secondary}{closer}"
        attr = NSMutableAttributedString.alloc().initWithString_(full)
        full_range = (0, len(full))
        attr.addAttribute_value_range_(
            NSFontAttributeName, NSFont.menuFontOfSize_(0), full_range
        )
        secondary_start = len(primary)
        secondary_len = len(joiner) + len(secondary) + len(closer)
        attr.addAttribute_value_range_(
            NSForegroundColorAttributeName,
            NSColor.secondaryLabelColor(),
            (secondary_start, secondary_len),
        )
        self._refresh_item._menuitem.setAttributedTitle_(attr)

    def _update_credentials_item(self) -> None:
        """Render account row title/callback based on current auth state."""
        creds = load_credentials()
        email = creds.email.strip() if creds is not None else ""
        # Keep the account action congruent with the menubar headline:
        # whenever the title says "Sign In", the menu row must also be a
        # sign-in/fix-account action (never sign out), even if a stale daemon
        # session still exists somewhere in the background.
        needs_sign_in = (
            (getattr(self, "_title_value", "") == "⚠ Sign In")
            or (not email)
            or self._last_daemon_status in {"auth_failed", "tos_required", "logged_out"}
        )
        menu_item = self._creds_item._menuitem
        if needs_sign_in:
            primary = _SIGN_IN_MENU_PRIMARY_TITLE
            callback = self._on_first_run_credentials if not email else self._on_edit_credentials
        else:
            primary = _SIGN_OUT_MENU_PRIMARY_TITLE
            callback = self._on_logout_credentials
        if self._creds_item.callback is not callback:
            self._creds_item.set_callback(callback)
        if not email:
            menu_item.setAttributedTitle_(None)
            self._creds_item.title = primary
            return
        self._apply_credentials_attr_title(primary, email)

    def _apply_credentials_attr_title(self, primary: str, email: str) -> None:
        joiner = " ("
        closer = ")"
        full = f"{primary}{joiner}{email}{closer}"
        attr = NSMutableAttributedString.alloc().initWithString_(full)
        full_range = (0, len(full))
        attr.addAttribute_value_range_(
            NSFontAttributeName, NSFont.menuFontOfSize_(0), full_range
        )
        secondary_start = len(primary)
        secondary_len = len(joiner) + len(email) + len(closer)
        attr.addAttribute_value_range_(
            NSForegroundColorAttributeName,
            NSColor.secondaryLabelColor(),
            (secondary_start, secondary_len),
        )
        self._creds_item._menuitem.setAttributedTitle_(attr)

    def _session_refresh_then_tick(self, *, log_label: str) -> None:
        """POST /auth/refresh on the daemon, then pull current reading + graph."""
        log.info("%s", log_label)
        try:
            self._client.force_refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("LibreView session refresh failed: %s", e)
        _run_on_main_and_wait(lambda: self._on_tick(None))

    def _set_signing_in_state(self) -> None:
        """Render an explicit in-progress auth state in the menubar title."""
        # Keep account-row logic and headline in sync while auth is in flight.
        self._last_daemon_status = "starting"
        self._apply_title_parts("Signing in...", "", "", color=None)
        self._update_credentials_item()

    def _check_sign_in_status_after_restart(self) -> str | None:
        """Poll daemon status briefly after a credential save + restart.

        Returns a status string (for example ``ok``, ``stale``, ``auth_failed``)
        when one is observed before timeout, otherwise ``None``.
        """
        deadline = time.monotonic() + _AUTH_RETRY_CHECK_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                reading = self._client.current()
            except (DaemonNotReady, DaemonUnreachable, DaemonError):
                time.sleep(_AUTH_RETRY_CHECK_INTERVAL_S)
                continue
            if reading.status == "starting":
                time.sleep(_AUTH_RETRY_CHECK_INTERVAL_S)
                continue
            return reading.status
        return None

    def _handle_post_sign_in_status(self, status: str | None) -> bool:
        """Return True when sign-in can be treated as successful."""
        if status == "auth_failed":
            self._show_alert(
                title="Sign-in failed",
                message=(
                    "LibreView rejected that email or password. "
                    "Please check your credentials and try again."
                ),
            )
            return False
        if status == "tos_required":
            self._show_alert(
                title="Action needed in Libre app",
                message=(
                    "LibreView requires Terms/Privacy re-acceptance before "
                    "GlucoBar can connect. Open the official Libre app on your "
                    "phone, accept the prompts, then try again."
                ),
            )
            return False
        return True

    def _on_about(self, _sender) -> None:
        _show_about_panel()

    def _on_refresh(self, _sender) -> None:
        if not _has_stored_credentials():
            log.info("Refresh clicked while logged out; opening sign-in flow")
            _run_in_background(self._first_run_credentials_impl)
            return
        self._session_refresh_then_tick(log_label="Refresh")

    def _on_logout_credentials(self, _sender) -> None:
        log.info("Sign out clicked")
        _run_in_background(self._logout_credentials_impl)

    def _on_first_run_credentials(self, _sender) -> None:
        _run_in_background(self._first_run_credentials_impl)

    def _on_edit_credentials(self, _sender) -> None:
        _run_in_background(self._edit_credentials_impl)

    def _logout_credentials_impl(self) -> None:
        clear_credentials()
        if load_credentials() is not None:
            self._show_alert(
                title="Couldn't sign out",
                message=(
                    "GlucoBar couldn't remove your LibreView credentials from "
                    "the macOS Keychain. Please unlock Keychain Access and try again."
                ),
            )
            return
        _clear_credentials_on_supervisor()
        # Restart the daemon so any in-memory/disk session is dropped now.
        _kickstart_daemon()
        _run_on_main_and_wait(lambda: self._on_tick(None))
        _run_on_main_and_wait(self._update_credentials_item)
        # Reuse the first-run onboarding dialog immediately after sign-out.
        self._first_run_credentials_impl()

    def _on_first_run_tick(self, _timer) -> None:
        """Fire once, on the main thread, shortly after NSApp starts.

        Stops the timer immediately — it's a one-shot — then pops
        the same two-field credentials modal the menu uses, with
        onboarding copy. Rechecks the Keychain first because the
        user could in principle have seeded credentials out-of-band
        (``scripts/set_credentials.py``) between ``__init__`` and
        the first tick.
        """
        if self._first_run_timer is not None:
            self._first_run_timer.stop()
            self._first_run_timer = None
        if load_credentials() is not None:
            return
        log.info("No LibreView credentials in Keychain; showing onboarding modal")
        _run_in_background(self._first_run_credentials_impl)

    def _first_run_credentials_impl(self) -> None:
        """Run the onboarding credentials flow on a worker thread.

        Same code path as :meth:`_edit_credentials_impl` — the only
        differences are the dialog's body/button copy and the success alert
        (a friendly "you're signed in" message rather than the generic edit
        confirmation).
        """
        default_email = ""
        default_password = ""
        while True:
            def _show_dialog(
                default_email=default_email,
                default_password=default_password,
            ):
                return _native_credentials_dialog(
                    current_email=default_email,
                    current_password=default_password,
                    body_text=_SIGN_IN_DIALOG_BODY,
                    save_button=_SIGN_IN_DIALOG_BUTTON,
                )

            ok, email, password = _run_on_main_and_wait(
                _show_dialog
            )
            if not ok:
                log.info("First-run credentials prompt cancelled")
                return
            email = email.strip()
            default_email = email
            default_password = password
            if not email or not password:
                self._show_alert(
                    title="Sign-in incomplete",
                    message=(
                        "GlucoBar needs both your email and your LibreView "
                        "password. Please check both fields and try again."
                    ),
                )
                continue
            try:
                save_credentials(email=email, password=password)
            except Exception as e:  # noqa: BLE001
                log.exception("Could not save first-run credentials")
                self._show_alert(
                    title="Couldn't save sign-in",
                    message=(
                        f"{e}\n\nGlucoBar couldn't write to your Mac Keychain. "
                        "Please try again."
                    ),
                )
                continue
            log.info("Saved LibreView credentials from first-run prompt")
            _push_credentials_to_supervisor(email, password)
            _run_on_main_and_wait(self._update_credentials_item)
            _run_on_main_and_wait(self._set_signing_in_state)
            # Daemon is in the supervisor's sticky EX_CONFIG state; nudge
            # it so it respawns now with the new Keychain entry instead of
            # waiting for the user to poke the menu.
            _kickstart_daemon()
            status = self._check_sign_in_status_after_restart()
            if not self._handle_post_sign_in_status(status):
                continue
            # Immediately repaint headline state so the menubar title
            # doesn't lag behind the success modal copy.
            _run_on_main_and_wait(lambda: self._on_tick(None))
            self._show_alert(
                title="You're signed in",
                message=(
                    "GlucoBar is connecting to LibreView with your account now. "
                    "Your first glucose reading should appear in the menubar "
                    "shortly."
                ),
            )
            return

    def _update_startup_item(self) -> None:
        enabled = _frontend_startup_enabled()
        self._startup_item._menuitem.setState_(1 if enabled else 0)

    def _on_toggle_startup(self, _sender) -> None:
        _run_in_background(self._toggle_startup_impl)

    def _toggle_startup_impl(self) -> None:
        uid = os.getuid()
        target = f"gui/{uid}/{_FRONTEND_LAUNCHD_LABEL}"
        enabled = _frontend_startup_enabled()
        if enabled:
            try:
                proc = _launchctl("disable", target)
            except Exception as e:  # noqa: BLE001
                self._show_alert(
                    title="Couldn't update startup setting",
                    message=f"{e}",
                )
                return
            if proc.returncode != 0:
                self._show_alert(
                    title="Couldn't update startup setting",
                    message=(proc.stderr or proc.stdout or "launchctl disable failed").strip(),
                )
                return
            _run_on_main_and_wait(self._update_startup_item)
            return

        if not _FRONTEND_PLIST_PATH.exists():
            self._show_alert(
                title="Startup agent not installed",
                message=(
                    "LaunchAgent file not found at:\n"
                    f"{_FRONTEND_PLIST_PATH}\n\n"
                    "Run scripts/install_launchagent.sh install first."
                ),
            )
            return
        try:
            proc = _launchctl("enable", target)
        except Exception as e:  # noqa: BLE001
            self._show_alert(
                title="Couldn't update startup setting",
                message=f"{e}",
            )
            return
        if proc.returncode != 0:
            self._show_alert(
                title="Couldn't update startup setting",
                message=(proc.stderr or proc.stdout or "launchctl enable failed").strip(),
            )
            return
        _run_on_main_and_wait(self._update_startup_item)

    def _edit_credentials_impl(self) -> None:
        """Capture email + password and save to Keychain.

        Uses a single native ``NSPanel`` with two fields.
        """
        pref_email, pref_password = _credentials_dialog_prefill()
        while True:
            def _show_dialog(
                pref_email=pref_email,
                pref_password=pref_password,
            ):
                return _native_credentials_dialog(
                    current_email=pref_email,
                    current_password=pref_password,
                    body_text=_SIGN_IN_DIALOG_BODY,
                    save_button=_SIGN_IN_DIALOG_BUTTON,
                )

            ok, email, password = _run_on_main_and_wait(
                _show_dialog
            )
            if not ok:
                return

            email = email.strip()
            pref_email = email
            pref_password = password
            if not email:
                self._show_alert(
                    title="Email missing",
                    message="Enter the email for your LibreView account, then try again.",
                )
                continue
            if not password:
                self._show_alert(
                    title="Password missing",
                    message="Enter your LibreView password, then try again.",
                )
                continue

            try:
                save_credentials(email=email, password=password)
            except Exception as e:  # noqa: BLE001
                self._show_alert(
                    title="Couldn't save sign-in",
                    message=(
                        f"{e}\n\n"
                        "GlucoBar only reads LibreView credentials from the macOS "
                        "Keychain, so they need to land there before the daemon "
                        "can sign in."
                    ),
                )
                continue
            _push_credentials_to_supervisor(email, password)
            _run_on_main_and_wait(self._update_credentials_item)
            _run_on_main_and_wait(self._set_signing_in_state)
            # Kick the daemon so it re-authenticates with the new credentials
            # right away, instead of either waiting for its next poll (if
            # already running under the old creds) or for launchd's throttled
            # respawn (if it exited on missing-credentials before first run).
            _kickstart_daemon()
            status = self._check_sign_in_status_after_restart()
            if not self._handle_post_sign_in_status(status):
                continue
            # Keep menu-bar state in sync with this success path.
            _run_on_main_and_wait(lambda: self._on_tick(None))
            self._show_alert(
                title="Sign-in saved",
                message=(
                    "Saved to your Mac Keychain. "
                    "GlucoBar is reconnecting with these details now."
                ),
            )
            return

    def _show_alert(self, *, title: str, message: str) -> None:
        """Show an informational alert via native ``NSPanel``.

        Uses :func:`_native_info_alert` (an ``NSPanel``) instead of
        ``NSAlert`` so layout matches our other custom modals.
        """
        _run_on_main_and_wait(
            lambda: _native_info_alert(title=title, message=message)
        )


def main() -> int:
    load_dotenv(_REPO_ROOT / ".env")
    _ensure_bundled_launch()
    _hide_dock_icon_by_default()
    base_url = os.environ.get("DAEMON_URL", "http://127.0.0.1:8765")
    poll_interval = _FRONTEND_POLL_SECONDS
    thresholds = ColorThresholds(
        low=_THRESHOLD_LOW_MG_DL,
        high=_THRESHOLD_HIGH_MG_DL,
        very_high=_THRESHOLD_VERY_HIGH_MG_DL,
    )

    # Start the daemon supervisor before GlucoseApp.__init__. With no
    # credentials in the Keychain the supervised daemon will exit 78
    # (EX_CONFIG) on spawn and the supervisor marks its sticky
    # "config-error" state. The first-run credentials modal runs later
    # from inside the rumps runloop (see ``_on_first_run_tick``), and
    # its success path calls ``request_restart()`` which clears the
    # sticky flag so the daemon respawns with the saved creds.
    #
    # The supervisor probes ``${DAEMON_URL}/health`` first and goes into
    # passive "external" mode if something else is already responding
    # (dev workflow: ``python -m daemon`` in another terminal), so there
    # is no separate opt-out for the supervisor — it's always wired up
    # and makes the right choice based on what's actually listening.
    from macos_frontend.daemon_supervisor import (
        DaemonSupervisor,
        SupervisorConfig,
        set_active,
    )
    supervisor = DaemonSupervisor(SupervisorConfig(base_url=base_url))
    # Read Keychain once, in the frontend, and hand credentials to the
    # supervisor so the daemon subprocess never triggers a second macOS
    # Keychain authorisation prompt. Missing credentials are fine — the
    # daemon will exit EX_CONFIG and the menubar's first-run onboarding
    # path (see ``GlucoseApp._on_first_run_tick``) will collect them and
    # push them back to the supervisor via ``set_credentials``.
    try:
        boot_creds = load_credentials()
    except Exception:  # noqa: BLE001
        boot_creds = None
        log.exception("Could not read Keychain at startup")
    if boot_creds is not None:
        supervisor.set_credentials(boot_creds.email, boot_creds.password)
    try:
        supervisor.start()
    except Exception:  # noqa: BLE001 — frontend must still start
        log.exception(
            "Daemon supervisor failed to start; continuing without one. "
            "The frontend will keep reconnecting via DAEMON_URL."
        )
    else:
        set_active(supervisor)
        log.info("Daemon supervisor active in %s mode", supervisor.mode)

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
