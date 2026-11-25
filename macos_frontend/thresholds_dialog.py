"""Native ``NSPanel`` for editing custom notification thresholds.

Mirrors the credentials dialog in ``app.py``: a titled panel (no
``NSAlert`` icon slot), Return = Save, Escape = Cancel. Values are shown
and typed in the account's display unit; parsing and validation live in
``macos_frontend.notifications`` so they stay unit-testable.

Must run on the main thread.
"""

from __future__ import annotations

from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSButton,
    NSFont,
    NSLayoutConstraint,
    NSMakeRect,
    NSPanel,
    NSTextAlignmentRight,
    NSTextField,
    NSView,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject


class _ThresholdsDialogController(NSObject):
    def save_(self, _sender):  # noqa: N802 - Obj-C selector name.
        NSApp.stopModalWithCode_(1)

    def cancel_(self, _sender):  # noqa: N802 - Obj-C selector name.
        NSApp.stopModalWithCode_(0)


def show_thresholds_dialog(
    *, unit_label: str, low: str, high: str, very_high: str
) -> tuple[bool, str, str, str]:
    """Return ``(saved, low_text, high_text, very_high_text)``."""
    h_padding = 20
    v_padding = 20
    label_width = 150
    field_width = 80
    content_width = 340

    body = NSTextField.wrappingLabelWithString_(
        "Gluco notifies you when your glucose goes below Low or above High "
        "or Very high. These values only affect notifications, not the "
        "menu bar colors."
    )
    body.setFont_(NSFont.systemFontOfSize_(NSFont.systemFontSize()))
    body.setTranslatesAutoresizingMaskIntoConstraints_(False)

    content = NSView.alloc().init()
    content.addSubview_(body)

    rows = []
    fields = []
    for title, value in (
        ("Low — below", low),
        ("High — above", high),
        ("Very high — above", very_high),
    ):
        label = NSTextField.labelWithString_(title)
        label.setAlignment_(NSTextAlignmentRight)
        label.setTranslatesAutoresizingMaskIntoConstraints_(False)
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, field_width, 22))
        field.setStringValue_(value)
        field.setTranslatesAutoresizingMaskIntoConstraints_(False)
        unit = NSTextField.labelWithString_(unit_label)
        unit.setTranslatesAutoresizingMaskIntoConstraints_(False)
        for v in (label, field, unit):
            content.addSubview_(v)
        rows.append((label, field, unit))
        fields.append(field)

    controller = _ThresholdsDialogController.alloc().init()
    save_btn = NSButton.buttonWithTitle_target_action_("Save", controller, "save:")
    save_btn.setKeyEquivalent_("\r")
    save_btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
    cancel_btn = NSButton.buttonWithTitle_target_action_("Cancel", controller, "cancel:")
    cancel_btn.setKeyEquivalent_("\x1b")
    cancel_btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
    content.addSubview_(cancel_btn)
    content.addSubview_(save_btn)

    constraints = [
        body.leadingAnchor().constraintEqualToAnchor_constant_(
            content.leadingAnchor(), h_padding
        ),
        body.trailingAnchor().constraintEqualToAnchor_constant_(
            content.trailingAnchor(), -h_padding
        ),
        body.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), v_padding),
        body.widthAnchor().constraintEqualToConstant_(content_width - 2 * h_padding),
    ]
    above = body.bottomAnchor()
    gap = 16
    for label, field, unit in rows:
        constraints += [
            field.topAnchor().constraintEqualToAnchor_constant_(above, gap),
            field.widthAnchor().constraintEqualToConstant_(field_width),
            field.leadingAnchor().constraintEqualToAnchor_constant_(
                content.leadingAnchor(), h_padding + label_width + 8
            ),
            label.leadingAnchor().constraintEqualToAnchor_(body.leadingAnchor()),
            label.widthAnchor().constraintEqualToConstant_(label_width),
            label.firstBaselineAnchor().constraintEqualToAnchor_(field.firstBaselineAnchor()),
            unit.leadingAnchor().constraintEqualToAnchor_constant_(field.trailingAnchor(), 6),
            unit.firstBaselineAnchor().constraintEqualToAnchor_(field.firstBaselineAnchor()),
        ]
        above = field.bottomAnchor()
        gap = 8
    constraints += [
        save_btn.trailingAnchor().constraintEqualToAnchor_(body.trailingAnchor()),
        save_btn.topAnchor().constraintEqualToAnchor_constant_(above, 20),
        save_btn.bottomAnchor().constraintEqualToAnchor_constant_(
            content.bottomAnchor(), -v_padding
        ),
        cancel_btn.trailingAnchor().constraintEqualToAnchor_constant_(
            save_btn.leadingAnchor(), -8
        ),
        cancel_btn.centerYAnchor().constraintEqualToAnchor_(save_btn.centerYAnchor()),
    ]
    NSLayoutConstraint.activateConstraints_(constraints)

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, content_width, 240),
        NSWindowStyleMaskTitled,
        NSBackingStoreBuffered,
        False,
    )
    panel.setTitle_("Notification Thresholds")
    panel.setContentView_(content)
    panel.setReleasedWhenClosed_(False)
    panel.setInitialFirstResponder_(fields[0])
    # Tab moves Low → High → Very high.
    fields[0].setNextKeyView_(fields[1])
    fields[1].setNextKeyView_(fields[2])
    fields[2].setNextKeyView_(fields[0])
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
    return (
        int(code) == 1,
        fields[0].stringValue() or "",
        fields[1].stringValue() or "",
        fields[2].stringValue() or "",
    )
