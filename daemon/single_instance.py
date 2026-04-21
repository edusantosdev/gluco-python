"""Prevent two daemons from racing against the same LibreLinkUp account.

A misfiring LaunchAgent or a manual double-launch will produce two
processes that both authenticate, both poll, and trip Abbott's
rate limiter. We hold an exclusive ``flock`` on a sentinel file for
the lifetime of the process; if another daemon already owns it,
``acquire_lock`` exits the process cleanly.

The lock is auto-released when the process exits (the kernel drops
``flock``s along with the file descriptor), so no atexit handler is
required.
"""

from __future__ import annotations

import fcntl
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Module-level so the GC doesn't close the fd while the daemon is running.
_lock_fd: int | None = None


def acquire_lock(path: Path) -> None:
    """Acquire an exclusive lock on ``path``; exit(1) if another holder exists."""
    global _lock_fd
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        log.error(
            "Another glucose daemon is already running (lockfile: %s). "
            "Refusing to start a second instance.",
            path,
        )
        sys.exit(1)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _lock_fd = fd
    log.info("Acquired single-instance lock at %s (pid=%d)", path, os.getpid())
