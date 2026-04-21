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
from pathlib import Path

import rumps
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


def _format_status_line(reading: CurrentReading) -> str:
    if reading.status == "ok":
        age = reading.reading_age_seconds
        return f"Status: ok ({reading.unit or ''}) — sensor age {age}s"
    if reading.status == "stale":
        age = reading.reading_age_seconds
        return f"Status: stale — sensor age {age}s"
    return f"Status: {reading.status.replace('_', ' ')}"


class GlucoseApp(rumps.App):
    def __init__(self, base_url: str, poll_interval: int):
        super().__init__("···", quit_button="Quit")
        self.poll_interval = poll_interval

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

    # --- timer / menu callbacks ---

    def _on_tick(self, _sender) -> None:
        try:
            reading = self._client.current()
        except DaemonUnreachable as e:
            log.warning("Daemon unreachable: %s", e)
            self.title = "⚠ daemon"
            self._status_item.title = "Status: daemon unreachable"
            return
        except DaemonError as e:
            log.warning("Daemon error: %s", e)
            self.title = "⚠ daemon"
            self._status_item.title = f"Status: daemon error — {e}"
            return

        self.title = _format_title(reading)
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
    log.info("Starting frontend (daemon=%s, poll=%ss)", base_url, poll_interval)
    GlucoseApp(base_url=base_url, poll_interval=poll_interval).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
