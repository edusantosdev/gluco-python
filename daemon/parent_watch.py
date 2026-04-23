"""Watch a parent process via a POSIX pipe, self-terminate on EOF.

Background
----------
When the macOS frontend supervises the daemon as a child process, we
need the daemon to die if the frontend dies — otherwise
``launchctl bootout`` (or an AppKit-initiated Quit) will orphan the
daemon to PID 1 and leave it holding port 8765 and the
``daemon.lock`` sentinel. Python signal handlers installed in the
frontend aren't reliable under PyObjC / AppKit's runloop (signals
can be swallowed by NSApplication), so we can't depend on the parent
running its own teardown.

The approach here is a pure-POSIX idiom that works on Linux too:

1. Before ``fork`` / ``exec``, the supervisor creates an anonymous
   pipe (``os.pipe()``).
2. The child inherits the read end. The supervisor keeps the write
   end open and never writes to it — it's purely a liveness channel.
3. The child's end of the fd is given to this module via the
   ``GLUCOBAR_PARENT_PIPE_FD`` environment variable.
4. This module spawns a single daemon thread that does a blocking
   ``os.read(fd, 1)``. As long as the supervisor is alive, the read
   blocks. When the supervisor dies (for any reason — SIGTERM,
   SIGKILL, panic, the machine unplugged, anything), the kernel
   closes the last reference to the pipe's write end, and the
   child's read returns ``b""`` (EOF).
5. On EOF the watcher sends ``SIGTERM`` to our own PID, which
   triggers the daemon's existing graceful-shutdown path
   (``daemon.main._install_signal_handlers``) — the poller stops,
   uvicorn tears down, the lockfile is released.

Why not ``PR_SET_PDEATHSIG`` / ``kqueue(EVFILT_PROC)``?
  Both work, but each is platform-specific: ``PR_SET_PDEATHSIG`` is
  Linux-only; ``kqueue`` is BSD/macOS-only. The pipe trick is
  portable POSIX so the daemon can keep a single codepath and the
  Plasma port stays clean (systemd isn't going to pass
  ``GLUCOBAR_PARENT_PIPE_FD``, so ``enable_from_env`` becomes a
  no-op there — which is what we want, systemd is the supervisor).
"""

from __future__ import annotations

import logging
import os
import signal
import threading

log = logging.getLogger(__name__)

# Environment variable name, shared with the supervisor side.
# Value is the integer fd the supervisor passed into the child.
ENV_FD = "GLUCOBAR_PARENT_PIPE_FD"


def enable_from_env() -> bool:
    """Start a parent-death watcher if ``ENV_FD`` is set in os.environ.

    Safe to call unconditionally during daemon startup. Returns True
    if a watcher was started, False if the env var was unset or
    unparseable. Errors during setup are logged, not raised —
    self-cleanup is a best-effort safety net, not a critical path.
    """
    raw = os.environ.get(ENV_FD)
    if not raw:
        return False
    try:
        fd = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; ignoring", ENV_FD, raw)
        return False
    if fd < 0:
        log.warning("%s=%d looks invalid; ignoring", ENV_FD, fd)
        return False
    _start_watcher(fd)
    return True


def _start_watcher(fd: int) -> None:
    """Spin up a daemon thread that blocks on ``fd`` and SIGTERMs on EOF."""
    # Clear the env var so grandchildren don't inherit it and race us.
    # (Unlikely — the daemon doesn't spawn children — but cheap insurance
    # against future refactors.)
    os.environ.pop(ENV_FD, None)

    thread = threading.Thread(
        target=_watch, args=(fd,), name="parent-death-watch", daemon=True
    )
    thread.start()
    log.info("Parent-death watcher armed on fd=%d (pid=%d)", fd, os.getpid())


def _watch(fd: int) -> None:
    """Block on the pipe. On EOF, SIGTERM ourselves; on error, give up.

    Under normal supervision the read blocks indefinitely. On EOF
    (parent closed their write end, i.e. exited) read returns b"".
    Any other exception (EBADF if the fd was closed, EIO, …) also
    breaks the loop; we log and exit the watcher without tearing
    ourselves down, because it's better to keep running under an
    unexpected error than to self-kill on a false positive.
    """
    try:
        data = os.read(fd, 1)
    except OSError as e:
        log.warning("parent-death watch aborted: %s", e)
        return
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    if data:
        # Shouldn't happen — we never write to the pipe. Treat any
        # non-empty read as "something's wrong" and keep running.
        log.warning(
            "parent-death pipe yielded unexpected data (%r); ignoring", data
        )
        return

    log.info("Parent pipe EOF; sending SIGTERM to self for clean shutdown")
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except OSError as e:
        log.error("Could not SIGTERM self on parent death: %s", e)
