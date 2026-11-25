"""Deliver :class:`~macos_frontend.notifications.Alert` s as macOS notifications.

Uses ``UNUserNotificationCenter`` (the replacement for the deprecated
``NSUserNotification`` that ``rumps.notification`` wraps). It only works
inside a real app bundle with a ``CFBundleIdentifier``; anywhere else
(``python -m macos_frontend`` from a checkout, tests) alerts are logged
instead of shown.

Each alert category (range / rapid / signal) uses a fixed request
identifier, so a reminder replaces the previous banner of the same kind
in Notification Center rather than stacking up.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import objc
from Foundation import NSBundle, NSObject

from macos_frontend.notifications import Alert

log = logging.getLogger("glucose.notifier")

try:
    import UserNotifications as UN
except ImportError:  # pragma: no cover - dependency missing in a dev env
    UN = None

_THREAD_ID = "com.glucobar.glucose"
_BUNDLE_ID = "com.glucobar.frontend"


def _running_in_gluco_bundle() -> bool:
    # Any bundle id isn't enough: an unbundled ``python`` reports
    # ``org.python.python`` and UNUserNotificationCenter aborts the
    # process when used from it.
    try:
        return NSBundle.mainBundle().bundleIdentifier() == _BUNDLE_ID
    except Exception:  # noqa: BLE001
        return False


if UN is not None:

    class _NotificationDelegate(
        NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]
    ):
        """Show banners even while Gluco is the active app.

        Without this, macOS silently drops notifications for the frontmost
        app — which Gluco briefly is whenever one of its dialogs is open.
        """

        def userNotificationCenter_willPresentNotification_withCompletionHandler_(  # noqa: N802
            self, _center, _notification, handler
        ):
            handler(
                UN.UNNotificationPresentationOptionBanner
                | UN.UNNotificationPresentationOptionList
                | UN.UNNotificationPresentationOptionSound
            )

        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(  # noqa: N802
            self, _center, _response, handler
        ):
            handler()


class Notifier:
    def __init__(self) -> None:
        self._center = None
        self._delegate = None
        if UN is None:
            log.warning("UserNotifications framework unavailable; alerts will only be logged")
            return
        if not _running_in_gluco_bundle():
            log.warning("Not running from Gluco.app; alerts will only be logged")
            return
        try:
            self._center = UN.UNUserNotificationCenter.currentNotificationCenter()
            self._delegate = _NotificationDelegate.alloc().init()
            self._center.setDelegate_(self._delegate)
        except Exception:  # noqa: BLE001
            log.exception("Could not set up UNUserNotificationCenter")
            self._center = None

    @property
    def available(self) -> bool:
        return self._center is not None

    def request_authorization(self, on_result: Callable[[bool], None] | None = None) -> None:
        """Ask macOS for permission (prompts only the first time).

        ``on_result(granted)`` runs on a background queue.
        """
        if self._center is None:
            if on_result is not None:
                on_result(False)
            return

        def _done(granted, error):
            if error is not None:
                log.warning("Notification authorization error: %s", error)
            log.info("Notification authorization granted=%s", bool(granted))
            if on_result is not None:
                try:
                    on_result(bool(granted))
                except Exception:  # noqa: BLE001
                    log.exception("Notification authorization callback failed")

        options = UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound
        self._center.requestAuthorizationWithOptions_completionHandler_(options, _done)

    def send(self, alert: Alert, *, sound: bool) -> None:
        log.info("Alert [%s] %s — %s", alert.kind, alert.title, alert.body)
        if self._center is None:
            return
        try:
            content = UN.UNMutableNotificationContent.alloc().init()
            content.setTitle_(alert.title)
            content.setBody_(alert.body)
            content.setThreadIdentifier_(_THREAD_ID)
            if sound:
                content.setSound_(UN.UNNotificationSound.defaultSound())
            if alert.time_sensitive and hasattr(content, "setInterruptionLevel_"):
                # Honoured only when the app carries the time-sensitive
                # entitlement; otherwise macOS delivers it as "active".
                content.setInterruptionLevel_(
                    UN.UNNotificationInterruptionLevelTimeSensitive
                )
            request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                f"gluco.{alert.category}", content, None
            )
        except Exception:  # noqa: BLE001
            log.exception("Could not build notification")
            return

        def _added(error):
            if error is not None:
                log.warning("Notification not delivered: %s", error)

        self._center.addNotificationRequest_withCompletionHandler_(request, _added)
