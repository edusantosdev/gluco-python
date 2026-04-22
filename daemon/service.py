"""Background polling service that owns the pylibrelinkup client.

Threading model
---------------

``pylibrelinkup`` is synchronous; its calls block on network I/O. Rather
than scatter ``asyncio.to_thread`` calls through async route handlers,
we run the poller in a dedicated daemon thread. FastAPI handlers stay
``async`` and do non-blocking reads from a lock-guarded ``DaemonState``
snapshot. This decouples poll cadence from request latency entirely.

Robustness features
-------------------

* Disk-backed session persistence via :class:`FileSessionStore`, so
  daemon restarts don't trigger a fresh LibreView login.
* :class:`ExponentialBackoff` for network errors and rate-limit waits,
  with a dedicated instance per failure mode so one doesn't penalise
  the other.
* One-shot auth refresh on ``AuthenticationError`` inside a poll — we
  drop the cached JWT, re-authenticate, retry once, and then fall
  through to backoff if that still fails.
* Sensor warm-up detection from the ``Sensor`` metadata (``a`` +
  ``w``-minutes vs now).
"""

from __future__ import annotations

import logging
import threading
import time
import warnings
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

from .backoff import ExponentialBackoff
from .session_store import FileSessionStore
from .state import DaemonState, Status

log = logging.getLogger(__name__)


# Wait-after policy for states that shouldn't participate in normal backoff:
# TOS_REQUIRED can't self-heal — the user needs to click accept — so we
# just poke it every few minutes.
_BACKOFF_ON_TOS_SECONDS = 300


class GlucoseService:
    def __init__(
        self,
        email: str,
        password: str,
        region: APIUrl,
        poll_interval: int,
        stale_after_seconds: int,
        session_store: FileSessionStore | None = None,
        error_backoff: ExponentialBackoff | None = None,
        ratelimit_backoff: ExponentialBackoff | None = None,
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

        self._session_store = session_store
        self._error_backoff = error_backoff or ExponentialBackoff(
            base_seconds=10, cap_seconds=300
        )
        self._ratelimit_backoff = ratelimit_backoff or ExponentialBackoff(
            base_seconds=30, cap_seconds=600
        )

    # --- lifecycle ---

    def start(self) -> None:
        """Restore session (if any), authenticate if needed, launch the poller."""
        if self._try_restore_session():
            # We have a valid cached JWT; patient list still requires a
            # round-trip but at least we skip the password submission.
            try:
                self._load_patient()
            except AuthenticationError:
                # Cached token turned out to be invalid — purge and re-auth.
                self._clear_session()
                self._authenticate_once(force=True)
            except Exception as e:  # noqa: BLE001
                log.warning("Post-restore patient fetch failed: %s", e)
                # Leave self.patient = None; _tick will re-attempt.
        else:
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
            # ``replace`` produces a new instance; the embedded ``reading``
            # reference is shared but immutable in practice.
            snap = replace(self._state)
            snap.reading = self._state.reading  # explicit, not deep-copied
            return snap

    def force_refresh(self) -> DaemonState:
        """Re-authenticate (dropping any cached session) and poll once.

        Used by ``POST /auth/refresh``. Runs on the caller's thread so the
        HTTP response reflects the new state."""
        self._clear_session()
        self._authenticate_once(force=True)
        if self.patient is not None:
            try:
                self._poll_once()
            except Exception as e:  # noqa: BLE001
                self._handle_poll_error(e)
        return self.snapshot()

    # --- internals: state mutation ---

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

    # --- internals: auth / session ---

    def _try_restore_session(self) -> bool:
        """If a valid session exists on disk, apply it to the client.

        Returns True iff the client is now authenticated (JWT set)."""
        if self._session_store is None:
            return False
        session = self._session_store.load()
        if session is None:
            return False
        self.client.token = session.token
        self.client.account_id_hash = session.account_id_hash
        log.info(
            "Restored LibreView session from disk (expires in %ds)",
            int(session.expires_at - time.time()),
        )
        return True

    def _persist_session(self) -> None:
        if self._session_store is None:
            return
        if not self.client.token or not self.client.account_id_hash:
            return
        try:
            self._session_store.save(self.client.token, self.client.account_id_hash)
        except OSError as e:
            log.warning("Could not persist session: %s", e)

    def _clear_session(self) -> None:
        if self._session_store is not None:
            self._session_store.clear()

    def _load_patient(self) -> None:
        """Fetch & cache the first patient. Raises on failure."""
        patients = self.client.get_patients()
        if not patients:
            self._set_state(
                status=Status.AUTH_FAILED,
                last_error="No patients shared with this account",
            )
            self.patient = None
            return
        self.patient = patients[0]
        log.info(
            "Using patient=%s %s",
            self.patient.first_name,
            self.patient.last_name,
        )
        self._set_state(last_error=None)

    def _authenticate_once(self, *, force: bool = False) -> bool:
        """Do a full login round-trip. Persist the new session on success."""
        try:
            if force:
                # Re-instantiate so any cached JWT inside the client is dropped.
                self.client = PyLibreLinkUp(
                    email=self._email, password=self._password, api_url=self._region
                )
            self.client.authenticate()
            self._persist_session()
            self._load_patient()
            return self.patient is not None
        except (TermsOfUseError, PrivacyPolicyError) as e:
            self._set_state(status=Status.TOS_REQUIRED, last_error=str(e))
        except AuthenticationError as e:
            self._clear_session()
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

    # --- internals: poll loop ---

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
                return self._error_backoff.next_delay()

        try:
            self._poll_once()
            self._error_backoff.reset()
            self._ratelimit_backoff.reset()
            return self.poll_interval
        except AuthenticationError:
            # JWT expired mid-session. Drop the cached one and try once more
            # right now — if that also fails, fall through to normal backoff.
            log.info("Auth expired; refreshing and retrying once")
            self._clear_session()
            if self._authenticate_once(force=True):
                try:
                    self._poll_once()
                    self._error_backoff.reset()
                    return self.poll_interval
                except Exception as e:  # noqa: BLE001
                    self._handle_poll_error(e)
            return self._error_backoff.next_delay()
        except LLUAPIRateLimitError as e:
            retry = getattr(e, "retry_after", None)
            # Trust the server hint when provided; fall back to our own
            # escalating schedule so repeated 429s keep stretching.
            delay = float(retry) if retry else self._ratelimit_backoff.next_delay()
            self._set_state(
                status=Status.RATE_LIMITED,
                last_error=f"retry_after={retry or int(delay)}s",
                bump_failures=True,
            )
            return delay
        except (TermsOfUseError, PrivacyPolicyError) as e:
            self._set_state(
                status=Status.TOS_REQUIRED, last_error=str(e), bump_failures=True
            )
            return _BACKOFF_ON_TOS_SECONDS
        except Exception as e:  # noqa: BLE001
            self._handle_poll_error(e)
            return self._error_backoff.next_delay()

    def _poll_once(self) -> None:
        """Hit the ``/graph`` endpoint once and fold the result into state.

        We request the full graph response (not just ``latest()``) because
        an empty ``graph_data`` list is our most reliable signal that the
        sensor is in its warm-up window — Libre publishes the start metadata
        but no measurements yet. ``latest()`` would raise or yield stale data.
        """
        assert self.patient is not None  # invariant: caller checks
        # ``pylibrelinkup.read()`` is deprecated but still the only public
        # method that returns the full GraphResponse (with connection +
        # sensor metadata we need for warm-up detection). ``graph()`` drops
        # the connection envelope. We silence the deprecation warning.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            graph_response = self.client.read(self.patient)

        now = datetime.now(UTC)
        connection = getattr(graph_response.data, "connection", None)
        history = getattr(graph_response.data, "graph_data", None) or []

        # Warm-up check: sensor metadata present, but no measurements yet.
        # After warm-up, ``graph_data`` is populated on every poll.
        if connection is not None and not history:
            if self._is_in_warmup(connection):
                self._set_state(
                    status=Status.WARMING_UP,
                    reading=None,
                    fetched_at=now,
                    last_error=None,
                    reset_failures=True,
                )
                return

        reading = getattr(connection, "glucose_measurement", None)
        if reading is None:
            # Unusual: no warm-up, no reading, no measurements. Treat as stale.
            self._set_state(
                status=Status.STALE,
                fetched_at=now,
                last_error="LibreView returned no current measurement",
                bump_failures=True,
            )
            return

        new_status = self._classify(reading, now)
        self._set_state(
            status=new_status,
            reading=reading,
            fetched_at=now,
            last_error=None,
            reset_failures=True,
        )

    # --- internals: classification ---

    def _is_in_warmup(self, connection: object) -> bool:
        """True iff the sensor is still inside its warm-up window.

        The FreeStyle Libre ``Sensor`` record carries:
            * ``a``: activation time in unix seconds
            * ``w``: warm-up duration in **minutes** (60 for Libre 2/3)
        Readings during this window aren't published; the consumer should
        show a friendly "warming up" indicator rather than "no data".
        """
        sensor = getattr(connection, "sensor", None)
        if sensor is None:
            return False
        activation = getattr(sensor, "a", None)
        warmup_minutes = getattr(sensor, "w", None)
        if not activation or not warmup_minutes:
            return False
        ready_at = activation + (warmup_minutes * 60)
        return ready_at > datetime.now(UTC).timestamp()

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
