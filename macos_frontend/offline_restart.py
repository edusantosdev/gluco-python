"""Decide when a long "⚠ Offline" stall warrants restarting the daemon (unit-testable).

The daemon recovers from network outages on its own via error backoff,
but it can occasionally wedge in ``no_network`` (or stop answering HTTP)
after the outage has cleared — typically around sleep/wake. A daemon
restart fixes that, so the menubar asks for one when "Offline" persists.

To stay gentle during a genuine, long outage the wait between restarts
doubles after each attempt (capped), and only resets once the daemon
reports something other than offline. ``time.monotonic`` doesn't advance
while the Mac sleeps, so sleep time never counts towards a stall.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# Menubar statuses (``GlucoseApp._last_daemon_status``) that render as
# "⚠ Offline" — the daemon reporting ``no_network`` and the frontend
# failing to reach the daemon both map here.
OFFLINE_STATUSES = frozenset({"no_network"})

# Transient states a restart itself produces ("Connecting…"). They neither
# count as offline nor as recovery, so a restart that lands straight back
# in "Offline" keeps escalating the wait instead of resetting it.
NEUTRAL_STATUSES = frozenset({"starting"})

DEFAULT_FIRST_RESTART_AFTER_S = 10 * 60.0
DEFAULT_MAX_RESTART_AFTER_S = 60 * 60.0


class OfflineRestartPolicy:
    """Feed it each poll's status; it says when to restart the daemon."""

    def __init__(
        self,
        *,
        first_restart_after_s: float = DEFAULT_FIRST_RESTART_AFTER_S,
        max_restart_after_s: float = DEFAULT_MAX_RESTART_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._first_after = first_restart_after_s
        self._max_after = max_restart_after_s
        self._clock = clock
        self._wait = first_restart_after_s
        self._offline_since: float | None = None

    def observe(self, status: str) -> bool:
        """Record one poll's status; return True if the daemon should restart now."""
        if status in NEUTRAL_STATUSES:
            return False
        if status not in OFFLINE_STATUSES:
            self._offline_since = None
            self._wait = self._first_after
            return False

        now = self._clock()
        if self._offline_since is None:
            self._offline_since = now
            return False
        if now - self._offline_since < self._wait:
            return False

        # Restart now, and restart the stall clock with a longer wait in
        # case this is a real outage that no restart can fix.
        self._offline_since = now
        self._wait = min(self._wait * 2, self._max_after)
        return True
