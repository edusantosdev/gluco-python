"""Unit tests for the exponential-backoff helper.

Focus: the *shape* of the sequence (base, 2x, 4x, ..., cap) is preserved
under each jitter policy, and reset() actually restarts the sequence.
"""

from __future__ import annotations

from daemon.backoff import ExponentialBackoff, seed_for_tests


def test_no_jitter_is_deterministic_and_capped() -> None:
    b = ExponentialBackoff(base_seconds=1, cap_seconds=8, factor=2, jitter="none")
    seq = [b.next_delay() for _ in range(6)]
    assert seq == [1, 2, 4, 8, 8, 8]


def test_reset_restarts_sequence() -> None:
    b = ExponentialBackoff(base_seconds=2, cap_seconds=100, factor=2, jitter="none")
    b.next_delay()  # 2
    b.next_delay()  # 4
    b.next_delay()  # 8
    assert b.attempts == 3
    b.reset()
    assert b.attempts == 0
    assert b.next_delay() == 2


def test_equal_jitter_stays_in_band() -> None:
    """Equal-jitter ensures at least half the deterministic delay is kept."""
    b = ExponentialBackoff(base_seconds=10, cap_seconds=1000, factor=2, jitter="equal")
    seed_for_tests(b, seed=1)
    for expected_exp in (10, 20, 40, 80, 160):
        delay = b.next_delay()
        assert expected_exp / 2 <= delay <= expected_exp


def test_full_jitter_stays_in_band() -> None:
    b = ExponentialBackoff(base_seconds=10, cap_seconds=1000, factor=2, jitter="full")
    seed_for_tests(b, seed=42)
    for expected_exp in (10, 20, 40, 80):
        delay = b.next_delay()
        assert 0 <= delay <= expected_exp


def test_cap_applies_before_jitter() -> None:
    """Past the cap the random draw should never exceed cap."""
    b = ExponentialBackoff(base_seconds=1, cap_seconds=4, factor=2, jitter="full")
    seed_for_tests(b, seed=0)
    # Advance well past the cap.
    for _ in range(10):
        delay = b.next_delay()
        assert delay <= 4


def test_independent_instances_do_not_share_state() -> None:
    a = ExponentialBackoff(base_seconds=1, cap_seconds=64, jitter="none")
    b = ExponentialBackoff(base_seconds=1, cap_seconds=64, jitter="none")
    a.next_delay()
    a.next_delay()
    a.next_delay()
    assert b.next_delay() == 1  # not 8 — independent
