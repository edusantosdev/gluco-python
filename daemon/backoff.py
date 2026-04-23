"""Exponential backoff with jitter — pure, deterministic, easy to test.

The poller uses a single ``ExponentialBackoff`` instance per failure mode
(e.g. network error vs rate-limit) so that a burst of transient failures
doesn't immediately escalate to the cap, and a single success collapses
the wait back to ``base_seconds``.

Design notes
------------
* "Full jitter" (Amazon's formulation): ``sleep = random_between(0, exp)``.
  Gives better worst-case spread than "equal jitter" for independent
  clients. We support it via ``jitter="full"``; the default is "equal"
  (``exp/2 + random(0, exp/2)``) which is a bit more predictable in
  single-client scenarios like this personal daemon.
* ``attempt()`` returns the *next* sleep duration **and** advances state.
  ``reset()`` must be called after success.
* The sequence is ``base, 2*base, 4*base, ...`` capped at ``cap_seconds``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ExponentialBackoff:
    base_seconds: float = 5.0
    cap_seconds: float = 300.0
    factor: float = 2.0
    jitter: Literal["equal", "full", "none"] = "equal"
    _attempts: int = field(default=0, init=False, repr=False)
    # Injected for tests; defaults to module-level ``random.random``.
    _rng: random.Random = field(default_factory=random.Random, init=False, repr=False)

    def reset(self) -> None:
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def next_delay(self) -> float:
        """Compute the next sleep duration and advance the attempt counter."""
        exp = min(self.cap_seconds, self.base_seconds * (self.factor**self._attempts))
        self._attempts += 1
        if self.jitter == "none":
            return exp
        if self.jitter == "full":
            return self._rng.uniform(0.0, exp)
        # "equal": keep at least half the deterministic delay so we still
        # back off meaningfully, but spread the other half.
        return exp / 2 + self._rng.uniform(0.0, exp / 2)


def seed_for_tests(backoff: ExponentialBackoff, seed: int) -> None:
    """Give tests deterministic jitter without monkey-patching ``random``."""
    backoff._rng = random.Random(seed)
