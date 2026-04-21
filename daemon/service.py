"""Background polling service that owns the pylibrelinkup client.

Threading model
---------------

``pylibrelinkup`` is synchronous; its calls block on network I/O. Rather
than scatter ``asyncio.to_thread`` calls through async route handlers,
we run the poller in a dedicated daemon thread. FastAPI handlers stay
``async`` and do non-blocking reads from a lock-guarded ``DaemonState``
snapshot. This decouples poll cadence from request latency entirely.

Phase 3 adds:
    * disk-backed session persistence (``FileSessionStore``)
    * proper exponential backoff with jitter
    * rotating log file
    * sensor warm-up / sensor-ended detection

Phase 2 ships with the simplest viable versions of those: in-memory
session, fixed retry interval on failure, console logging only.
"""

from __future__ import annotations

import logging
import threading
from copy import copy
from dataclasses import replace
from datetime import UTC, datetime

from pylibrelinkup import (
    APIUrl,
    AuthenticationError,
    LLUAPIRateLimitError,
    PrivacyPolicyError,
    PyLibreLinkUp,
    TermsOfUseError,
)

from .state import DaemonState, Status

log = logging.getLogger(__name__)

# Conservative fallback when an exception type doesn't carry retry guidance.
_BACKOFF_ON_ERROR_SECONDS = 30
_BACKOFF_ON_TOS_SECONDS = 300


class GlucoseService:
    def __init__(
        self,
        email: str,
        password: str,
        region: APIUrl,
        poll_interval: int,
        stale_after_seconds: int,
    ) -> None:
        self._email = email
        self._password = password
        self._region = region
        self.poll_interval = poll_interval
        self.stale_after = stale_after_seconds

        self.client = PyLibreLinkUp(email=email, password=password, api_url=region)
        self.patient = None

        self._state = DaemonState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle ---

    def start(self) -> None:
        """Authenticate (best-effort) and launch the poller thread."""
        if not self._authenticate_once():
            log.warning(
                "Initial authentication failed (status=%s). Daemon will keep retrying.",
                self.snapshot().status.value,
            )
        self._thread = threading.Thread(
            target=self._poll_loop, name="glucose-poller", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the poller to exit and wait briefly for it to do so."""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("Poller thread did not exit within %.1fs", timeout)

    # --- public API used by FastAPI handlers ---

    def snapshot(self) -> DaemonState:
        """Return a thread-safe shallow copy of current state."""
        with self._lock:
            # ``replace`` on a dataclass produces a new instance; the embedded
            # ``reading`` reference is shared but immutable in practice.
            snap = replace(self._state)
            snap.reading = self._state.reading  # explicit, not deep-copied
            return snap

    def force_refresh(self) -> DaemonState:
        """Re-authenticate and poll once on the calling thread.

        Used by ``POST /auth/refresh``. Runs synchronously so the caller
        gets the new state in the same response."""
        self._authenticate_once(force=True)
        if self.patient is not None:
            try:
                self._poll_once()
            except Exception as e:  # noqa: BLE001
                self._handle_poll_error(e)
        return self.snapshot()

    # --- internals ---

    def _set_state(
        self,
        *,
        status: Status | None = None,
        reading: object | None = None,
        fetched_at: datetime | None = None,
        last_error: str | None = ...,  # type: ignore[assignment]
        bump_failures: bool = False,
        reset_failures: bool = False,
    ) -> None:
        with self._lock:
            if status is not None:
                self._state.status = status
            if reading is not None:
                self._state.reading = reading
            if fetched_at is not None:
                self._state.fetched_at = fetched_at
            if last_error is not ...:
                self._state.last_error = last_error
            if bump_failures:
                self._state.consecutive_failures += 1
            if reset_failures:
                self._state.consecutive_failures = 0

    def _authenticate_once(self, *, force: bool = False) -> bool:
        try:
            if force:
                # Re-instantiate so any cached JWT inside the client is dropped.
                self.client = PyLibreLinkUp(
                    email=self._email, password=self._password, api_url=self._region
                )
            self.client.authenticate()
            patients = self.client.get_patients()
            if not patients:
                self._set_state(
                    status=Status.AUTH_FAILED,
                    last_error="No patients shared with this account",
                )
                self.patient = None
                return False
            self.patient = patients[0]
            log.info(
                "Authenticated; patient=%s %s",
                self.patient.first_name,
                self.patient.last_name,
            )
            self._set_state(last_error=None)
            return True
        except (TermsOfUseError, PrivacyPolicyError) as e:
            self._set_state(status=Status.TOS_REQUIRED, last_error=str(e))
        except AuthenticationError as e:
            self._set_state(status=Status.AUTH_FAILED, last_error=str(e))
        except LLUAPIRateLimitError as e:
            self._set_state(
                status=Status.RATE_LIMITED,
                last_error=f"retry_after={getattr(e, 'retry_after', '?')}s",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Unexpected auth error")
            self._set_state(status=Status.NO_NETWORK, last_error=f"{type(e).__name__}: {e}")
        self.patient = None
        return False

    def _poll_loop(self) -> None:
        log.info("Poller started (interval=%ss)", self.poll_interval)
        while not self._stop.is_set():
            sleep_for = self._tick()
            # ``Event.wait`` returns immediately when ``stop`` is set, giving
            # us a clean way to interrupt sleeps on shutdown.
            self._stop.wait(sleep_for)
        log.info("Poller stopped")

    def _tick(self) -> float:
        """One iteration of the poll loop. Returns seconds to sleep."""
        snap_status = self.snapshot().status

        if snap_status == Status.TOS_REQUIRED:
            # Don't hammer Abbott while waiting for the user to click accept.
            # Re-attempting quietly every 5 minutes lets us recover automatically
            # once the user has resolved it on their phone.
            if not self._authenticate_once():
                return _BACKOFF_ON_TOS_SECONDS

        if self.patient is None:
            if not self._authenticate_once():
                return _BACKOFF_ON_ERROR_SECONDS

        try:
            self._poll_once()
            return self.poll_interval
        except AuthenticationError:
            log.info("Auth expired; re-authenticating")
            self._authenticate_once(force=True)
            return min(self.poll_interval, _BACKOFF_ON_ERROR_SECONDS)
        except LLUAPIRateLimitError as e:
            retry = getattr(e, "retry_after", None) or _BACKOFF_ON_ERROR_SECONDS
            self._set_state(
                status=Status.RATE_LIMITED,
                last_error=f"retry_after={retry}s",
                bump_failures=True,
            )
            return float(retry)
        except (TermsOfUseError, PrivacyPolicyError) as e:
            self._set_state(status=Status.TOS_REQUIRED, last_error=str(e), bump_failures=True)
            return _BACKOFF_ON_TOS_SECONDS
        except Exception as e:  # noqa: BLE001
            self._handle_poll_error(e)
            return _BACKOFF_ON_ERROR_SECONDS

    def _poll_once(self) -> None:
        assert self.patient is not None  # invariant: caller checks
        reading = self.client.latest(self.patient)
        now = datetime.now(UTC)
        new_status = self._classify(reading, now)
        self._set_state(
            status=new_status,
            reading=reading,
            fetched_at=now,
            last_error=None,
            reset_failures=True,
        )

    def _classify(self, reading: object, now: datetime) -> Status:
        # ``factory_timestamp`` is the tz-aware UTC one; ``timestamp`` is
        # naive *local* time, despite the lack of tzinfo. Using the wrong
        # one produces a 1+ hour offset on TZ != UTC.
        ts = getattr(reading, "factory_timestamp", None) or getattr(reading, "timestamp", None)
        if ts is None:
            return Status.OK
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = (now - ts).total_seconds()
        if age > self.stale_after:
            return Status.STALE
        return Status.OK

    def _handle_poll_error(self, e: Exception) -> None:
        log.warning("Poll failed: %s: %s", type(e).__name__, e)
        self._set_state(
            status=Status.NO_NETWORK,
            last_error=f"{type(e).__name__}: {e}",
            bump_failures=True,
        )
