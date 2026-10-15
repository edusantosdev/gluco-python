"""Supervise the daemon as a child process of the menubar app.

Why this module exists
----------------------
Gluco ships as a single ``.app``. The menubar is the user-facing
entry point; the daemon is a background poll loop. Shipping them as
one launchd job (the frontend) with the daemon as a supervised child
means:

  - One installed LaunchAgent. One quit takes everything down, and
    there is no possibility of a "ghost daemon" running after the
    menubar exits.
  - The HTTP seam between frontend and daemon stays intact, so the
    KDE/Plasma port can run the same daemon unchanged under
    ``systemd --user`` and talk to it from Qt. This supervisor is
    macOS-specific glue — it replaces what ``systemd`` will do on
    Linux, not the daemon itself.

Dev workflow is preserved. If a daemon is already responding at
``DAEMON_URL`` when the frontend starts (for example, because you ran
``python -m daemon`` in another terminal), the supervisor connects to
it in **external** mode and does not spawn a child. If nothing is
listening, it enters **managed** mode and owns the daemon's lifecycle.

Lifecycle
---------
``start()``:
  1. Probe ``${DAEMON_URL}/health``. If it responds, record "external"
     mode and return.
  2. Otherwise, spawn ``sys.executable -m daemon`` as a child with its
     stdio redirected to files under the state dir (so a crashed
     child's stderr survives for debugging even if launchd already
     rotated the frontend's). Wait up to ``startup_timeout_seconds``
     for health to come up.
  3. Start a watchdog thread that respawns the child if it exits.
     Exit code 78 (``EX_CONFIG`` — the daemon's signal for "no
     credentials") is treated as a sticky "needs user action" state;
     we stop auto-restarting until something calls ``restart()``
     explicitly (e.g. after the user saves credentials through the
     menubar).

``stop()``: SIGTERM the child, wait up to ``shutdown_timeout_seconds``,
then SIGKILL. Registered as an ``atexit`` hook so quitting through
``rumps`` → ``NSApplication terminate:`` still tears down cleanly.

``restart()``: SIGTERM the child and wait for it to exit; the watchdog
respawns it within one throttle interval. Clears the sticky
config-error state so a daemon that died with exit 78 can be brought
back after credentials have been fixed, and revives the watchdog if it
had given up after too many crashes.

Tests
-----
``probe_fn`` and ``spawn_fn`` are dependency-injected so unit tests can
swap in fakes; see ``tests/test_daemon_supervisor.py``.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

log = logging.getLogger(__name__)


# Exit code the daemon uses for "credentials missing / misconfigured"
# (see ``daemon/main.py`` — it mirrors BSD's ``EX_CONFIG``). Worth a
# named constant here so the watchdog's handling is obvious.
_EX_CONFIG = 78

# How fast is "too fast" for a restart loop? If the daemon crashes
# this many times inside this window we stop auto-restarting and wait
# for an explicit ``restart()``. Defaults match launchd's own
# ``ThrottleInterval`` ergonomics.
_CRASH_WINDOW_SECONDS = 60.0
_CRASH_LIMIT = 5


SpawnFn = Callable[..., subprocess.Popen]
ProbeFn = Callable[[str, float], bool]


def _default_daemon_executable() -> str:
    """Return the best path to run ``-m daemon`` as.

    Inside the bundled ``Gluco.app`` the build step (see
    ``scripts/build_portable_app.sh``) places a ``GlucoDaemon``
    copy of the Python launcher next to py2app's generic ``python``.
    Using the renamed binary means any macOS permission dialog
    triggered by the daemon subprocess identifies as
    ``GlucoDaemon`` instead of a bare ``python``, which is
    noticeably clearer for end users. Outside the bundle
    (source checkouts, dev runs), fall back to ``sys.executable``.
    """
    exe = Path(sys.executable)
    candidate = exe.with_name("GlucoDaemon")
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate)
    return sys.executable


@dataclass
class SupervisorConfig:
    """Knobs for :class:`DaemonSupervisor`.

    Kept separate from the supervisor class so tests can build one
    without having to construct a full runtime (no real subprocess,
    no real HTTP probes). Fields map 1:1 to the supervisor's behavior:

    * ``base_url`` — where the frontend will also talk to the daemon.
    * ``state_dir`` — receives ``daemon.stdout.log`` / ``.stderr.log``.
    * ``python_executable`` — what to spawn as argv[0]. Defaults to
      ``sys.executable``, which under a packaged ``.app`` is the bundle
      launcher symlink (that in turn points at the venv's Python),
      so the child inherits the same interpreter + site-packages.
    * ``working_directory`` — the repo root by default, so the daemon's
      ``pydantic-settings`` finds ``.env`` when present.
    """

    base_url: str = "http://127.0.0.1:8765"
    state_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("DAEMON_STATE_DIR", Path.home() / ".glucobar")
        )
    )
    python_executable: str = field(default_factory=lambda: _default_daemon_executable())
    working_directory: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent.parent
    )
    startup_timeout_seconds: float = 15.0
    shutdown_timeout_seconds: float = 5.0
    probe_interval_seconds: float = 0.25
    watchdog_restart_delay_seconds: float = 2.0


def _default_probe(base_url: str, timeout: float) -> bool:
    """Return True if ``${base_url}/health`` is reachable.

    Uses stdlib ``urllib`` so the supervisor has zero runtime deps
    beyond what's already on ``sys.path`` — important because this
    runs before ``httpx`` / ``rumps`` are definitely usable (e.g. we
    may spawn the daemon before the frontend's imports complete).
    """
    try:
        with urlopen(f"{base_url.rstrip('/')}/health", timeout=timeout) as r:
            return 200 <= r.status < 500
    except URLError:
        return False
    except Exception as e:  # noqa: BLE001 — probe errors must never bubble
        log.debug("daemon health probe raised %s; treating as unreachable", e)
        return False


def _default_spawn(
    argv: list[str],
    working_dir: Path,
    stdout,
    stderr,
    *,
    pass_fds: tuple[int, ...] = (),
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Fork the daemon with its own process group.

    A new process group lets us send a single signal to the entire
    child (uvicorn + polling thread + any grandchildren) rather than
    chasing them individually. ``setsid`` is the portable way to do
    that; macOS supports it fine.

    ``pass_fds`` / ``env`` are threaded through so the parent-death
    pipe (see :mod:`daemon.parent_watch`) can be handed to the child.
    """
    return subprocess.Popen(  # noqa: S603 — argv assembled from trusted config
        argv,
        cwd=str(working_dir),
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        pass_fds=pass_fds,
        env=env,
    )


class DaemonSupervisor:
    """Own the daemon subprocess when the frontend is the process manager.

    Thread-safe: all state mutations go through ``self._lock``, which
    the public methods and the watchdog thread share.
    """

    # Modes reflect *ownership*, not health. In ``external`` the
    # supervisor is a passive observer — it doesn't restart, doesn't
    # stop, doesn't claim the daemon's PID.
    MODE_STOPPED = "stopped"
    MODE_MANAGED = "managed"
    MODE_EXTERNAL = "external"

    def __init__(
        self,
        config: SupervisorConfig | None = None,
        *,
        probe_fn: ProbeFn = _default_probe,
        spawn_fn: SpawnFn = _default_spawn,
    ) -> None:
        self._config = config or SupervisorConfig()
        self._probe_fn = probe_fn
        self._spawn_fn = spawn_fn

        self._lock = threading.Lock()
        self._mode: str = self.MODE_STOPPED
        self._child: subprocess.Popen | None = None
        self._stdout_file = None
        self._stderr_file = None
        # Credentials injected into the daemon's env, so the daemon
        # process never touches macOS Keychain directly (see
        # ``glucose_common.credentials``).
        self._injected_env: dict[str, str] = {}
        # Write end of the parent-death pipe. We keep this fd open and
        # never write to it; when *this process* exits (for any reason
        # — clean, SIGTERM, SIGKILL, crash), the kernel closes it and
        # the child's corresponding read end sees EOF. That's the
        # signal the child uses to self-terminate, closing the
        # launchctl-orphan loophole. ``None`` means no watch active
        # (external mode, or spawn hasn't happened yet).
        self._parent_pipe_write_fd: int | None = None
        self._watchdog: threading.Thread | None = None
        self._shutdown_requested = threading.Event()
        # Sticky: set True when the daemon exits 78. Cleared by
        # ``restart()``. While True the watchdog treats child exit as
        # terminal and stops relaunching.
        self._config_error = False
        # Rolling record of recent crash timestamps, used to enforce
        # the ``_CRASH_LIMIT`` within ``_CRASH_WINDOW_SECONDS``.
        self._recent_exits: list[float] = []
        self._atexit_registered = False

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_credentials(self, email: str, password: str) -> None:
        """Inject LibreView credentials into subsequent daemon spawns.

        The frontend reads credentials from Keychain once and calls this
        before ``start()`` (and again on Sign In) so the daemon child
        never needs to re-read Keychain — collapsing the macOS auth
        prompt to a single appearance tied to the main ``Gluco``
        executable.
        """
        from glucose_common.credentials import ENV_EMAIL, ENV_PASSWORD

        with self._lock:
            self._injected_env = {
                ENV_EMAIL: email,
                ENV_PASSWORD: password,
            }

    def clear_credentials(self) -> None:
        """Drop any injected credentials (e.g. after Sign Out)."""
        with self._lock:
            self._injected_env = {}

    @property
    def is_managed(self) -> bool:
        return self.mode == self.MODE_MANAGED

    def start(self) -> None:
        """Detect an existing daemon or spawn a new one.

        Blocks until either the existing daemon responds, the new
        child becomes healthy, or ``startup_timeout_seconds`` elapses.
        Raises ``TimeoutError`` if we spawned a child that never
        reached healthy state — the caller decides whether to surface
        that as a UI error or let the frontend start anyway and rely
        on its own reconnect loop.
        """
        with self._lock:
            if self._mode != self.MODE_STOPPED:
                log.debug("DaemonSupervisor.start() ignored (mode=%s)", self._mode)
                return

        # Probe first. A quick timeout here is important: if nobody is
        # listening we want to move on to spawning, not block the UI.
        if self._probe_fn(self._config.base_url, 0.5):
            with self._lock:
                self._mode = self.MODE_EXTERNAL
            log.info(
                "Daemon already reachable at %s; running in external mode",
                self._config.base_url,
            )
            return

        self._ensure_state_dir()
        self._spawn_and_wait()
        self._install_signal_handlers()
        self._register_atexit()
        self._start_watchdog()

    def stop(self, timeout: float | None = None) -> None:
        """Terminate the managed child, if any."""
        with self._lock:
            if self._mode != self.MODE_MANAGED:
                self._mode = self.MODE_STOPPED
                return
            self._shutdown_requested.set()
            child = self._child
            self._child = None
            self._mode = self.MODE_STOPPED
        if child is not None:
            self._terminate(child, timeout or self._config.shutdown_timeout_seconds)
        # Closing the write end here triggers EOF in the child's
        # parent-death watcher as a belt-and-braces signal — even if
        # SIGTERM got swallowed somewhere, the pipe still fires.
        self._close_parent_pipe()
        self._close_log_files()

    def restart(self) -> None:
        """Respawn the daemon and block until the old process has exited.

        Only meaningful in managed mode; in external mode this is a no-op
        (see :func:`request_restart`). Blocking until the old child is
        gone means a caller polling the daemon right afterwards can't read
        the *old* process's status (e.g. a stale ``auth_failed`` from the
        previous credentials). Call from a worker thread, not the UI.

        If the watchdog has given up (crash limit hit, or a respawn
        failed), this also clears the crash history, spawns a fresh child
        and restarts the watchdog, so an explicit user action can always
        bring the daemon back.
        """
        with self._lock:
            if self._mode != self.MODE_MANAGED:
                log.debug("DaemonSupervisor.restart() ignored (mode=%s)", self._mode)
                return
            self._config_error = False  # Let the watchdog try again.
            child = self._child
            watchdog_alive = self._watchdog is not None and self._watchdog.is_alive()
            if not watchdog_alive:
                self._recent_exits.clear()

        if child is not None and child.poll() is None:
            log.info("Restarting supervised daemon (pid=%s)", child.pid)
            try:
                os.killpg(os.getpgid(child.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError) as e:
                log.warning("Could not SIGTERM daemon pid=%s: %s", child.pid, e)
            else:
                try:
                    child.wait(timeout=self._config.shutdown_timeout_seconds)
                except subprocess.TimeoutExpired:
                    log.warning("Daemon pid=%s still running after SIGTERM", child.pid)

        if not watchdog_alive:
            log.info("Watchdog inactive; spawning a fresh daemon")
            self._respawn()
            self._start_watchdog()

    # --- internals -------------------------------------------------------

    def _ensure_state_dir(self) -> None:
        self._config.state_dir.mkdir(parents=True, exist_ok=True)

    def _open_log_files(self):
        """Open append-mode log files for the child's stdio.

        Rotated by the shell, not by us — we don't want to invent a
        second rotation policy when the daemon itself already has one
        for its application log.
        """
        out_path = self._config.state_dir / "daemon.stdout.log"
        err_path = self._config.state_dir / "daemon.stderr.log"
        # Buffer size 1 = line-buffered in text mode, so partial
        # crashes still show up in the file without an extra flush.
        return (
            open(out_path, "a", buffering=1, encoding="utf-8"),
            open(err_path, "a", buffering=1, encoding="utf-8"),
        )

    def _close_log_files(self) -> None:
        for f in (self._stdout_file, self._stderr_file):
            if f is not None:
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass
        self._stdout_file = None
        self._stderr_file = None

    def _spawn_and_wait(self) -> None:
        argv = [self._config.python_executable, "-m", "daemon"]
        log.info("Spawning daemon: %s (cwd=%s)", argv, self._config.working_directory)

        self._stdout_file, self._stderr_file = self._open_log_files()
        try:
            child = self._spawn_child(argv)
        except Exception:
            self._close_log_files()
            raise

        self._child = child

        if not self._wait_for_health():
            # Health didn't come up. Don't kill the child here — the
            # watchdog will observe its exit or keep it alive while
            # the frontend runs. The frontend already tolerates a
            # temporarily-unreachable daemon via its own polling
            # backoff, so the most useful thing we can do is log
            # loudly and let the UI show "Connecting…".
            log.warning(
                "Daemon spawned (pid=%s) but /health not responding after %.1fs",
                child.pid,
                self._config.startup_timeout_seconds,
            )

        with self._lock:
            self._mode = self.MODE_MANAGED
        log.info("Daemon running under supervisor (pid=%s)", child.pid)

    def _wait_for_health(self) -> bool:
        deadline = time.monotonic() + self._config.startup_timeout_seconds
        while time.monotonic() < deadline:
            # If the child exited before going healthy, stop waiting.
            child = self._child
            if child is not None and child.poll() is not None:
                log.error(
                    "Daemon exited during startup with code %s",
                    child.returncode,
                )
                return False
            if self._probe_fn(self._config.base_url, 1.0):
                return True
            time.sleep(self._config.probe_interval_seconds)
        return False

    def _start_watchdog(self) -> None:
        t = threading.Thread(
            target=self._watchdog_loop,
            name="Gluco-daemon-watchdog",
            daemon=True,
        )
        self._watchdog = t
        t.start()

    def _watchdog_loop(self) -> None:
        """Respawn the daemon when it exits, with crash-rate limiting."""
        while not self._shutdown_requested.is_set():
            with self._lock:
                child = self._child
            if child is None:
                return
            try:
                rc = child.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                continue
            # Child exited — decide whether to respawn.
            if self._shutdown_requested.is_set():
                return
            log.warning("Supervised daemon exited with code %s", rc)

            if rc == _EX_CONFIG:
                log.error(
                    "Daemon reported missing credentials (exit 78). "
                    "Waiting for explicit restart()."
                )
                with self._lock:
                    self._config_error = True
                    self._child = None
                # Block until either shutdown or restart() is called.
                # Restart clears ``_config_error``; we detect that
                # flip and re-enter the spawn path.
                self._wait_for_config_error_clear()
                if self._shutdown_requested.is_set():
                    return

            if self._exceeded_crash_limit():
                log.error(
                    "Daemon crashed %d times in %.0fs; giving up.",
                    _CRASH_LIMIT, _CRASH_WINDOW_SECONDS,
                )
                with self._lock:
                    self._child = None
                return

            time.sleep(self._config.watchdog_restart_delay_seconds)
            try:
                self._respawn()
            except Exception:
                log.exception("Watchdog respawn failed; stopping supervisor")
                return

    def _wait_for_config_error_clear(self) -> None:
        """Block until ``restart()`` clears the sticky config-error flag."""
        while not self._shutdown_requested.is_set():
            time.sleep(0.5)
            with self._lock:
                if not self._config_error:
                    return

    def _exceeded_crash_limit(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._recent_exits = [
                t for t in self._recent_exits if now - t < _CRASH_WINDOW_SECONDS
            ]
            self._recent_exits.append(now)
            return len(self._recent_exits) >= _CRASH_LIMIT

    def _respawn(self) -> None:
        argv = [self._config.python_executable, "-m", "daemon"]
        log.info("Respawning daemon: %s", argv)
        # Reopen log files in case the previous child closed them on
        # its way out (the parent owns the handles, but keeping this
        # defensive avoids a half-baked log state after many cycles).
        if self._stdout_file is None or self._stderr_file is None:
            self._stdout_file, self._stderr_file = self._open_log_files()
        child = self._spawn_child(argv)
        with self._lock:
            self._child = child
        # A respawn doesn't reset the health-timeout semantics of
        # start() — if the process keeps crashing we'll catch that
        # above. We do, however, want a short grace period before
        # the next watchdog iteration so wait() sees a live child.

    def _spawn_child(self, argv: list[str]) -> subprocess.Popen:
        """Spawn the daemon with a parent-death pipe wired into its env.

        The supervisor keeps the *write* end of an anonymous pipe and
        passes the *read* end into the child via ``pass_fds`` + the
        ``GLUCOBAR_PARENT_PIPE_FD`` env var. See
        :mod:`daemon.parent_watch` for how the child uses it.

        Each spawn allocates a fresh pipe so we don't share a single
        fd across successive crash-respawn cycles (closing the old
        write fd only after the new child is started would give us a
        brief window where a zombie watcher in the old child could
        race against the new one).
        """
        # Close any stale write fd from a previous spawn before opening
        # a new pipe — no leaks across respawn cycles.
        self._close_parent_pipe()
        read_fd, write_fd = os.pipe()
        # ``os.pipe()`` fds are non-inheritable (PEP 446). That's what we
        # want for the write end: if the child inherited a copy it would
        # never see EOF. The read end is handed over explicitly via
        # ``pass_fds`` below.
        self._parent_pipe_write_fd = write_fd

        child_env = os.environ.copy()
        from daemon.parent_watch import ENV_FD
        child_env[ENV_FD] = str(read_fd)
        with self._lock:
            injected = dict(self._injected_env)
        if injected:
            child_env.update(injected)

        try:
            child = self._spawn_fn(
                argv,
                self._config.working_directory,
                self._stdout_file,
                self._stderr_file,
                pass_fds=(read_fd,),
                env=child_env,
            )
        finally:
            # The child now owns its own copy of read_fd; close ours.
            os.close(read_fd)
        return child

    def _close_parent_pipe(self) -> None:
        if self._parent_pipe_write_fd is None:
            return
        try:
            os.close(self._parent_pipe_write_fd)
        except OSError:
            pass
        self._parent_pipe_write_fd = None

    def _terminate(self, child: subprocess.Popen, timeout: float) -> None:
        if child.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as e:
            log.debug("SIGTERM to daemon pid=%s failed: %s", child.pid, e)
            return
        try:
            child.wait(timeout=timeout)
            log.info("Daemon exited cleanly (pid=%s)", child.pid)
            return
        except subprocess.TimeoutExpired:
            pass
        log.warning("Daemon pid=%s did not exit in %.1fs; sending SIGKILL",
                    child.pid, timeout)
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            child.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            log.error("Daemon pid=%s survived SIGKILL? Leaking.", child.pid)

    def _register_atexit(self) -> None:
        if self._atexit_registered:
            return
        atexit.register(self.stop)
        self._atexit_registered = True

    def _install_signal_handlers(self) -> None:
        """Ensure SIGTERM / SIGINT tear down the supervised child.

        ``launchctl bootout`` (and the AppKit Quit menu) ultimately
        deliver SIGTERM to this process. Python's default handler
        just exits without running ``atexit``, which would leave the
        daemon child orphaned (reparented to launchd, still holding
        port 8765 and ``daemon.lock``). We install a small handler
        that stops the child cleanly and then re-exits through the
        normal shutdown path so ``atexit`` runs for everything else.

        No-op on the non-main thread (``signal.signal`` raises
        ``ValueError`` there), which is what we want during tests or
        if a caller wraps ``start()`` inside a worker thread.
        """
        prior_term = signal.getsignal(signal.SIGTERM)
        prior_int = signal.getsignal(signal.SIGINT)

        def _handler(signum, frame):  # noqa: ANN001
            log.info(
                "Supervisor received %s; stopping supervised daemon",
                signal.Signals(signum).name,
            )
            self.stop()
            # Chain any pre-existing handler (rumps or the interpreter
            # default) so the frontend's own teardown still runs.
            prior = prior_int if signum == signal.SIGINT else prior_term
            if callable(prior):
                try:
                    prior(signum, frame)
                except SystemExit:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("Prior %s handler raised", signum)
            raise SystemExit(0)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # Not on the main thread — rumps or the test harness
                # will handle cleanup through atexit instead.
                pass


# --- module-level active supervisor --------------------------------------
#
# A process-wide singleton so callers that don't hold the
# ``GlucoseApp`` instance (e.g. module-level helpers in app.py) can
# still address the running supervisor.

_active: DaemonSupervisor | None = None


def set_active(supervisor: DaemonSupervisor | None) -> None:
    global _active
    _active = supervisor


def get_active() -> DaemonSupervisor | None:
    return _active


def request_restart(base_url: str | None = None) -> None:
    """Ask the supervised daemon to restart.

    In Gluco's strict single-wrapper topology the daemon only
    ever runs as a managed child of the frontend supervisor, so the
    only supported restart path is:

      1. Active :class:`DaemonSupervisor` in managed mode — SIGTERM
         the child; the watchdog respawns it.

    If no managed supervisor is active (external mode, or the
    frontend hasn't finished booting yet) we log and return. The
    daemon will pick up fresh state on its next natural restart,
    and silently declining to nudge is strictly less disruptive
    than crashing the menubar.
    """
    sup = get_active()
    if sup is not None and sup.is_managed:
        sup.restart()
        return

    log.info(
        "request_restart: no managed supervisor active; skipping nudge. "
        "The daemon will pick up new state on its next natural restart."
    )
