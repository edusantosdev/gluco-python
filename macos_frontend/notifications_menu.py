"""The **Notifications ▸** submenu: toggles, choices and the thresholds dialog.

Owns the persisted :class:`NotificationPrefs` and keeps checkmarks and
labels in sync with them. Alert evaluation stays in ``GlucoseApp`` (it
has the poll data); this class only edits preferences and talks to the
:class:`~macos_frontend.notifier.Notifier` for permission and test
banners.

rumps callbacks run on the main thread; anything that opens a modal is
pushed to a worker thread first (same pattern as the credentials flow)
so the menu's tracking loop is released before the dialog appears.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import rumps

from macos_frontend.notifications import (
    DEFAULT_THRESHOLDS,
    REMIND_CHOICES_MIN,
    SIGNAL_LOSS_CHOICES_MIN,
    Alert,
    AlertThresholds,
    effective_thresholds,
    format_threshold,
    load_prefs,
    parse_threshold_input,
    save_prefs,
    unit_label,
    validate_thresholds,
)
from macos_frontend.notifier import Notifier

log = logging.getLogger("glucose.frontend.notifications")

_DENIED_TITLE = "Notifications are turned off for Gluco."
_DENIED_MESSAGE = (
    "To get glucose alerts, allow notifications for Gluco in "
    "System Settings > Notifications, then turn them on here again."
)
_UNAVAILABLE_TITLE = "Notifications aren’t available."
_UNAVAILABLE_MESSAGE = "Gluco can only show notifications when it runs as Gluco.app."


def _remind_title(minutes: int) -> str:
    return "Only Once" if minutes == 0 else f"Every {minutes} Minutes"


def _signal_title(minutes: int) -> str:
    return f"After {minutes} Minutes" if minutes < 60 else "After 1 Hour"


def _thresholds_text(t: AlertThresholds, unit: str | None) -> str:
    vals = " / ".join(format_threshold(v, unit) for v in (t.low, t.high, t.very_high))
    return f"{vals} {unit_label(unit)}"


class NotificationsMenu:
    def __init__(
        self,
        *,
        prefs_path: Path,
        notifier: Notifier,
        get_unit: Callable[[], str | None],
        show_alert: Callable[..., None],
        run_in_background: Callable[[Callable[[], None]], None],
        run_on_main_and_wait: Callable[[Callable[[], object]], object],
        show_thresholds_dialog: Callable[..., tuple[bool, str, str, str]],
    ) -> None:
        self._prefs_path = prefs_path
        self._notifier = notifier
        self._get_unit = get_unit
        self._show_alert = show_alert
        self._run_in_background = run_in_background
        self._run_on_main_and_wait = run_on_main_and_wait
        self._show_thresholds_dialog = show_thresholds_dialog
        self.prefs = load_prefs(prefs_path)

        self.item = rumps.MenuItem("Notifications")
        self._enabled_item = rumps.MenuItem(
            "Enable Notifications", callback=self._on_toggle_enabled
        )
        self._low_item = rumps.MenuItem("Low", callback=self._toggle("low"))
        self._high_item = rumps.MenuItem("High", callback=self._toggle("high"))
        self._very_high_item = rumps.MenuItem("Very High", callback=self._toggle("very_high"))
        self._rapid_item = rumps.MenuItem(
            "Rising or Falling Fast (↑ ↓)", callback=self._toggle("rapid")
        )

        self._signal_item = rumps.MenuItem("No New Data")
        self._signal_off_item = rumps.MenuItem("Off", callback=self._on_signal_off)
        self._signal_items: dict[int, rumps.MenuItem] = {
            m: rumps.MenuItem(_signal_title(m), callback=self._on_signal_choice(m))
            for m in SIGNAL_LOSS_CHOICES_MIN
        }
        self._signal_item.update([self._signal_off_item, None, *self._signal_items.values()])

        self._thresholds_item = rumps.MenuItem("Thresholds")
        self._default_thresholds_item = rumps.MenuItem(
            "Default", callback=self._on_default_thresholds
        )
        self._custom_thresholds_item = rumps.MenuItem(
            "Custom…", callback=self._on_custom_thresholds
        )
        self._thresholds_item.update(
            [self._default_thresholds_item, self._custom_thresholds_item]
        )

        self._remind_item = rumps.MenuItem("Repeat Alerts")
        self._remind_items: dict[int, rumps.MenuItem] = {
            m: rumps.MenuItem(_remind_title(m), callback=self._on_remind_choice(m))
            for m in REMIND_CHOICES_MIN
        }
        self._remind_item.update(list(self._remind_items.values()))

        self._sound_item = rumps.MenuItem("Play Sound", callback=self._toggle("sound"))
        self._test_item = rumps.MenuItem("Send Test Notification", callback=self._on_test)

        self.item.update([
            self._enabled_item,
            None,
            self._low_item,
            self._high_item,
            self._very_high_item,
            self._rapid_item,
            self._signal_item,
            None,
            self._thresholds_item,
            self._remind_item,
            self._sound_item,
            None,
            self._test_item,
        ])
        self.refresh()

        if self.prefs.enabled:
            # Silent when permission was already decided; re-prompts only
            # if macOS forgot (e.g. the bundle was re-signed).
            self._notifier.request_authorization()

    # state ---------------------------------------------------------------

    def refresh(self) -> None:
        """Sync checkmarks and labels to the prefs and the account unit."""
        p = self.prefs
        unit = self._get_unit()
        t = effective_thresholds(p)
        self._enabled_item.state = int(p.enabled)
        self._low_item.title = f"Low (below {format_threshold(t.low, unit)} {unit_label(unit)})"
        self._high_item.title = f"High (above {format_threshold(t.high, unit)} {unit_label(unit)})"
        self._very_high_item.title = (
            f"Very High (above {format_threshold(t.very_high, unit)} {unit_label(unit)})"
        )
        self._low_item.state = int(p.low)
        self._high_item.state = int(p.high)
        self._very_high_item.state = int(p.very_high)
        self._rapid_item.state = int(p.rapid)

        self._signal_item.title = (
            f"No New Data ({_signal_title(p.signal_loss_minutes).lower()})"
            if p.signal_loss
            else "No New Data (off)"
        )
        self._signal_off_item.state = int(not p.signal_loss)
        for m, item in self._signal_items.items():
            item.state = int(p.signal_loss and m == p.signal_loss_minutes)

        custom = AlertThresholds(
            low=p.custom_low_mg_dl, high=p.custom_high_mg_dl, very_high=p.custom_very_high_mg_dl
        )
        self._default_thresholds_item.title = (
            f"Default ({_thresholds_text(DEFAULT_THRESHOLDS, unit)})"
        )
        self._custom_thresholds_item.title = (
            f"Custom ({_thresholds_text(custom, unit)})…" if p.use_custom_thresholds else "Custom…"
        )
        self._default_thresholds_item.state = int(not p.use_custom_thresholds)
        self._custom_thresholds_item.state = int(p.use_custom_thresholds)

        for m, item in self._remind_items.items():
            item.state = int(m == p.remind_minutes)
        self._sound_item.state = int(p.sound)

    def _update(self, **changes) -> None:
        self.prefs = replace(self.prefs, **changes)
        try:
            save_prefs(self._prefs_path, self.prefs)
        except OSError:
            log.exception("Could not save notification prefs")
        self.refresh()

    def _update_on_main(self, **changes) -> None:
        self._run_on_main_and_wait(lambda: self._update(**changes))

    # callbacks -----------------------------------------------------------

    def _toggle(self, field: str) -> Callable[[object], None]:
        def _cb(_sender) -> None:
            self._update(**{field: not getattr(self.prefs, field)})

        return _cb

    def _on_signal_off(self, _sender) -> None:
        self._update(signal_loss=False)

    def _on_signal_choice(self, minutes: int) -> Callable[[object], None]:
        def _cb(_sender) -> None:
            self._update(signal_loss=True, signal_loss_minutes=minutes)

        return _cb

    def _on_remind_choice(self, minutes: int) -> Callable[[object], None]:
        def _cb(_sender) -> None:
            self._update(remind_minutes=minutes)

        return _cb

    def _on_default_thresholds(self, _sender) -> None:
        self._update(use_custom_thresholds=False)

    def _on_toggle_enabled(self, _sender) -> None:
        if self.prefs.enabled:
            self._update(enabled=False)
            return
        if not self._notifier.available:
            self._run_in_background(
                lambda: self._show_alert(title=_UNAVAILABLE_TITLE, message=_UNAVAILABLE_MESSAGE)
            )
            return
        # Optimistically on; reverted below if macOS says no.
        self._update(enabled=True)

        def _on_result(granted: bool) -> None:
            if granted:
                return
            self._update_on_main(enabled=False)
            self._show_alert(title=_DENIED_TITLE, message=_DENIED_MESSAGE)

        self._notifier.request_authorization(_on_result)

    def _on_test(self, _sender) -> None:
        if not self._notifier.available:
            self._run_in_background(
                lambda: self._show_alert(title=_UNAVAILABLE_TITLE, message=_UNAVAILABLE_MESSAGE)
            )
            return
        sound = self.prefs.sound

        def _on_result(granted: bool) -> None:
            if not granted:
                self._show_alert(title=_DENIED_TITLE, message=_DENIED_MESSAGE)
                return
            self._notifier.send(
                Alert(
                    category="test",
                    kind="test",
                    title="Gluco test notification",
                    body="Glucose alerts will look like this.",
                ),
                sound=sound,
            )

        self._notifier.request_authorization(_on_result)

    def _on_custom_thresholds(self, _sender) -> None:
        self._run_in_background(self._custom_thresholds_impl)

    def _custom_thresholds_impl(self) -> None:
        unit = self._get_unit()
        p = self.prefs
        texts = (
            format_threshold(p.custom_low_mg_dl, unit),
            format_threshold(p.custom_high_mg_dl, unit),
            format_threshold(p.custom_very_high_mg_dl, unit),
        )
        while True:
            def _show(texts=texts):
                return self._show_thresholds_dialog(
                    unit_label=unit_label(unit),
                    low=texts[0],
                    high=texts[1],
                    very_high=texts[2],
                )

            ok, low_s, high_s, very_s = self._run_on_main_and_wait(_show)
            if not ok:
                return
            texts = (low_s, high_s, very_s)
            values = [parse_threshold_input(s, unit) for s in texts]
            if any(v is None for v in values):
                self._show_alert(
                    title="Enter a number for each threshold.",
                    message=f"Use {unit_label(unit)}, for example "
                    f"{format_threshold(DEFAULT_THRESHOLDS.low, unit)}.",
                )
                continue
            low, high, very_high = values
            error = validate_thresholds(low, high, very_high, unit=unit)
            if error is not None:
                self._show_alert(title="Check your thresholds.", message=error)
                continue
            self._update_on_main(
                use_custom_thresholds=True,
                custom_low_mg_dl=round(low, 1),
                custom_high_mg_dl=round(high, 1),
                custom_very_high_mg_dl=round(very_high, 1),
            )
            return
