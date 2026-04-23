"""Tests for :mod:`daemon.parent_watch`.

The behavior we want to lock down:

1. No env var ⇒ ``enable_from_env`` is a no-op (and returns False),
   so a daemon running standalone or under systemd doesn't pay any
   cost for the macOS-specific death-watch path.
2. With ``GLUCOBAR_PARENT_PIPE_FD`` set, the watcher SIGTERMs ourselves
   when the *other* end of the pipe is closed (simulating supervisor
   death).
3. A bogus value for the env var is logged and ignored rather than
   raising — this code is a best-effort safety net and must not
   crash the daemon on its own.
"""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from daemon import parent_watch


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Every test starts with a clean env var slate."""
    monkeypatch.delenv(parent_watch.ENV_FD, raising=False)


def test_no_env_is_noop():
    """Absent env var = no watcher, no exceptions."""
    assert parent_watch.enable_from_env() is False


def test_bad_env_is_ignored(monkeypatch):
    """Non-integer value → log + return False, never raises."""
    monkeypatch.setenv(parent_watch.ENV_FD, "not-an-int")
    assert parent_watch.enable_from_env() is False


def test_negative_fd_is_ignored(monkeypatch):
    monkeypatch.setenv(parent_watch.ENV_FD, "-1")
    assert parent_watch.enable_from_env() is False


def test_eof_triggers_sigterm(monkeypatch):
    """Closing the write end must cause the watcher to send SIGTERM.

    We substitute ``os.kill`` with a recorder so the test doesn't
    actually terminate the pytest process; in the real daemon the
    signal drives the existing uvicorn/poller shutdown path, but
    that path is out of scope here.
    """
    signals_sent: list[tuple[int, int]] = []
    sent_event = threading.Event()

    def record_kill(pid: int, sig: int) -> None:
        signals_sent.append((pid, sig))
        sent_event.set()

    monkeypatch.setattr(parent_watch.os, "kill", record_kill)

    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setenv(parent_watch.ENV_FD, str(read_fd))
        assert parent_watch.enable_from_env() is True

        # Simulate supervisor death by closing the write end.
        os.close(write_fd)
        write_fd = -1  # don't double-close in finally

        assert sent_event.wait(timeout=2.0), \
            "watcher did not SIGTERM after EOF within 2s"
        assert len(signals_sent) == 1
        assert signals_sent[0] == (os.getpid(), signal.SIGTERM)
    finally:
        # Cleanup: the watcher thread closes read_fd itself on exit,
        # so only close write_fd if we never did.
        if write_fd >= 0:
            try:
                os.close(write_fd)
            except OSError:
                pass


def test_enable_clears_env_var(monkeypatch):
    """Env var is popped after arming, so grandchildren don't inherit it."""
    monkeypatch.setattr(parent_watch.os, "kill", lambda *a, **k: None)
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setenv(parent_watch.ENV_FD, str(read_fd))
        parent_watch.enable_from_env()
        assert parent_watch.ENV_FD not in os.environ
    finally:
        # Let the watcher thread wake up and exit cleanly.
        try:
            os.close(write_fd)
        except OSError:
            pass
        # Give the thread a tick to drain before pytest moves on.
        time.sleep(0.05)
