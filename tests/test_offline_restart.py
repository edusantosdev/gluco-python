"""Unit tests for :mod:`macos_frontend.offline_restart`."""

from macos_frontend.offline_restart import OfflineRestartPolicy


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _policy(clock: _Clock) -> OfflineRestartPolicy:
    return OfflineRestartPolicy(
        first_restart_after_s=600.0, max_restart_after_s=1800.0, clock=clock
    )


def test_no_restart_before_threshold() -> None:
    clock = _Clock()
    policy = _policy(clock)
    assert policy.observe("no_network") is False
    clock.now += 599
    assert policy.observe("no_network") is False


def test_restart_once_threshold_reached() -> None:
    clock = _Clock()
    policy = _policy(clock)
    policy.observe("no_network")
    clock.now += 600
    assert policy.observe("no_network") is True
    # Stall clock restarted: the very next offline poll doesn't re-trigger.
    clock.now += 30
    assert policy.observe("no_network") is False


def test_recovery_resets_stall() -> None:
    clock = _Clock()
    policy = _policy(clock)
    policy.observe("no_network")
    clock.now += 500
    assert policy.observe("ok") is False
    assert policy.observe("no_network") is False
    clock.now += 500
    assert policy.observe("no_network") is False


def test_wait_doubles_and_caps_while_still_offline() -> None:
    clock = _Clock()
    policy = _policy(clock)
    policy.observe("no_network")
    clock.now += 600
    assert policy.observe("no_network") is True
    # Restart briefly shows "Connecting…" — that must not reset the backoff.
    assert policy.observe("starting") is False
    clock.now += 1199
    assert policy.observe("no_network") is False
    clock.now += 1
    assert policy.observe("no_network") is True
    # Capped at 1800s from here on.
    clock.now += 1800
    assert policy.observe("no_network") is True
    clock.now += 1800
    assert policy.observe("no_network") is True


def test_recovery_resets_backoff() -> None:
    clock = _Clock()
    policy = _policy(clock)
    policy.observe("no_network")
    clock.now += 600
    assert policy.observe("no_network") is True
    policy.observe("ok")
    policy.observe("no_network")
    clock.now += 600
    assert policy.observe("no_network") is True


def test_other_problem_statuses_never_restart() -> None:
    clock = _Clock()
    policy = _policy(clock)
    for status in ("rate_limited", "auth_failed", "error", "logged_out", "starting"):
        policy.observe(status)
        clock.now += 10_000
        assert policy.observe(status) is False
