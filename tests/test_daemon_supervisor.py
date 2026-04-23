"""Tests for ``macos_frontend.daemon_supervisor``.

All tests inject fake spawn / probe callables so we never start a real
subprocess. The real subprocess path is exercised end-to-end by
launching the frontend; here we just verify the state machine.
"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path

import pytest

from macos_frontend.daemon_supervisor import (
    DaemonSupervisor,
    SupervisorConfig,
    get_active,
    request_restart,
    set_active,
)


class FakeChild:
    """Stand-in for ``subprocess.Popen`` with just enough surface.

    The supervisor interacts with the Popen object in three ways:

    * ``poll()`` — non-blocking "is it still alive?"
    * ``wait(timeout)`` — blocking wait with timeout
    * ``returncode`` — exit status
    * ``pid`` — stable identifier for logs / signal targeting

    We don't use the pid for actual signalling in tests (the
    supervisor's SIGTERM path is replaced via ``signal_handler``), so
    any pid is fine.
    """

    _next_pid = 10_000

    def __init__(self) -> None:
        FakeChild._next_pid += 1
        self.pid = FakeChild._next_pid
        self._exit_code: int | None = None
        self._exit_event = threading.Event()

    def poll(self):
        return self._exit_code

    def wait(self, timeout: float | None = None):
        if self._exit_event.wait(timeout=timeout):
            return self._exit_code
        import subprocess as _sp
        raise _sp.TimeoutExpired(cmd="fake", timeout=timeout)

    @property
    def returncode(self):
        return self._exit_code

    def simulate_exit(self, code: int) -> None:
        self._exit_code = code
        self._exit_event.set()


def _base_config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        base_url="http://127.0.0.1:65535",  # never listening
        state_dir=tmp_path,
        python_executable="/usr/bin/true",
        working_directory=tmp_path,
        startup_timeout_seconds=1.0,
        shutdown_timeout_seconds=0.5,
        probe_interval_seconds=0.05,
        watchdog_restart_delay_seconds=0.05,
    )


def _always_unreachable(_url: str, _timeout: float) -> bool:
    return False


def _always_healthy(_url: str, _timeout: float) -> bool:
    return True


def test_external_mode_when_daemon_already_reachable(tmp_path):
    """If the probe succeeds on startup, we never spawn a child."""
    spawned: list[list[str]] = []

    def spy_spawn(argv, cwd, stdout, stderr, **kwargs):  # noqa: ANN001
        spawned.append(argv)
        return FakeChild()

    sup = DaemonSupervisor(
        _base_config(tmp_path),
        probe_fn=_always_healthy,
        spawn_fn=spy_spawn,
    )
    sup.start()
    try:
        assert sup.mode == DaemonSupervisor.MODE_EXTERNAL
        assert spawned == [], "supervisor spawned despite external daemon"
    finally:
        sup.stop()


def test_managed_mode_spawns_and_waits_for_health(tmp_path):
    """Unreachable at probe-time → spawn, then wait for health to flip."""
    healthy = threading.Event()

    def probe(_url, _timeout):
        return healthy.is_set()

    child = FakeChild()
    spawn_calls: list[list[str]] = []

    def spawn(argv, cwd, stdout, stderr, **kwargs):  # noqa: ANN001
        spawn_calls.append(argv)
        # Flip health ~shortly after spawn so start() doesn't time out.
        def _flip():
            time.sleep(0.1)
            healthy.set()
        threading.Thread(target=_flip, daemon=True).start()
        return child

    sup = DaemonSupervisor(
        _base_config(tmp_path),
        probe_fn=probe,
        spawn_fn=spawn,
    )
    sup.start()
    try:
        assert sup.mode == DaemonSupervisor.MODE_MANAGED
        assert spawn_calls, "supervisor never spawned"
        assert spawn_calls[0][-2:] == ["-m", "daemon"]
    finally:
        child.simulate_exit(0)  # let watchdog's wait() return
        sup.stop()


def test_watchdog_restarts_on_crash(tmp_path):
    """If the child exits with a non-config code, watchdog respawns."""
    children: list[FakeChild] = []

    def spawn(argv, cwd, stdout, stderr, **kwargs):  # noqa: ANN001
        c = FakeChild()
        children.append(c)
        return c

    sup = DaemonSupervisor(
        _base_config(tmp_path),
        probe_fn=_always_unreachable,
        spawn_fn=spawn,
    )
    # Start doesn't require health, just a spawn — we expect the
    # startup-timeout path (probe keeps returning False). That's fine
    # for this test: we only care that a crash triggers a respawn.
    sup.start()
    try:
        assert len(children) == 1
        # Simulate a non-config crash.
        children[0].simulate_exit(1)
        # Watchdog should respawn within a few hundred ms.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(children) < 2:
            time.sleep(0.05)
        assert len(children) == 2, "watchdog did not respawn after crash"
    finally:
        # Terminate the new child so stop() returns quickly.
        children[-1].simulate_exit(0)
        sup.stop()


def test_config_error_is_sticky_until_restart(tmp_path):
    """Exit 78 stops the respawn loop; restart() resumes it."""
    children: list[FakeChild] = []

    def spawn(argv, cwd, stdout, stderr, **kwargs):  # noqa: ANN001
        c = FakeChild()
        children.append(c)
        return c

    sup = DaemonSupervisor(
        _base_config(tmp_path),
        probe_fn=_always_unreachable,
        spawn_fn=spawn,
    )
    sup.start()
    try:
        assert len(children) == 1
        # Simulate the "missing credentials" exit code.
        children[0].simulate_exit(78)
        # Give the watchdog time to observe the exit and mark sticky.
        time.sleep(0.3)
        assert len(children) == 1, \
            "watchdog respawned after EX_CONFIG; should have held"
        # Now the user "fixes credentials" and asks for a restart.
        # The watchdog must pick that up and spawn again.
        sup.restart()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(children) < 2:
            time.sleep(0.05)
        assert len(children) == 2, \
            "restart() did not clear sticky config-error state"
    finally:
        children[-1].simulate_exit(0)
        sup.stop()


def test_stop_is_idempotent_in_external_mode(tmp_path):
    """External mode doesn't own a child, so stop() is a no-op."""
    sup = DaemonSupervisor(
        _base_config(tmp_path),
        probe_fn=_always_healthy,
        spawn_fn=lambda *a, **k: pytest.fail("should not spawn"),  # noqa: ARG005
    )
    sup.start()
    sup.stop()
    sup.stop()  # second stop must not raise
    assert sup.mode == DaemonSupervisor.MODE_STOPPED


def test_request_restart_prefers_active_supervisor(tmp_path, monkeypatch):
    """``request_restart`` should call the supervisor, not launchctl."""
    restart_calls: list[int] = []

    class FakeSup:
        is_managed = True
        def restart(self) -> None:
            restart_calls.append(1)

    set_active(FakeSup())
    try:
        # If the supervisor branch is taken, subprocess.run must not run.
        import macos_frontend.daemon_supervisor as ds
        monkeypatch.setattr(
            ds.subprocess, "run",
            lambda *a, **k: pytest.fail("launchctl fallback ran"),  # noqa: ARG005
        )
        request_restart()
        assert restart_calls == [1]
    finally:
        set_active(None)


def test_request_restart_falls_back_to_launchctl(monkeypatch):
    """No active supervisor → launchctl kickstart."""
    set_active(None)
    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stderr = ""

    import macos_frontend.daemon_supervisor as ds
    def fake_run(argv, **kwargs):  # noqa: ANN001
        calls.append(list(argv))
        return FakeProc()

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    request_restart()
    assert len(calls) == 1
    assert calls[0][1:3] == ["kickstart", "-k"]
    assert calls[0][3].startswith("gui/")
    assert calls[0][3].endswith("/com.glucobar.daemon")


def test_spawn_passes_parent_death_pipe(tmp_path):
    """Supervisor must hand a readable pipe fd to the child via env + pass_fds.

    This is how we detect supervisor death — see daemon/parent_watch.py.
    The child's read end must appear in ``pass_fds`` and in the
    ``GLUCOBAR_PARENT_PIPE_FD`` env, and the value there must be the
    same integer. We don't actually run a real child; we just inspect
    the kwargs the supervisor passed to ``spawn_fn``.
    """
    from daemon.parent_watch import ENV_FD

    captured: dict = {}

    def spawn(argv, cwd, stdout, stderr, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return FakeChild()

    sup = DaemonSupervisor(
        _base_config(tmp_path),
        probe_fn=_always_unreachable,
        spawn_fn=spawn,
    )
    sup.start()
    try:
        pass_fds = captured.get("pass_fds") or ()
        env = captured.get("env") or {}
        assert pass_fds, "no pass_fds supplied to child"
        assert ENV_FD in env, f"{ENV_FD} missing from child env"
        assert int(env[ENV_FD]) == pass_fds[0], \
            "env var fd disagrees with pass_fds entry"
    finally:
        sup.stop()


def test_get_active_roundtrip():
    sentinel = object()
    set_active(sentinel)  # type: ignore[arg-type]
    try:
        assert get_active() is sentinel
    finally:
        set_active(None)


# Silence pytest warning about unused import when the module's signal
# constants aren't exercised. Signal handling is integration-tested by
# the real subprocess path, not here.
_ = signal
