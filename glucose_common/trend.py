"""Shared trend-arrow mapping.

The underlying ``pylibrelinkup.Trend`` enum uses five buckets:

    DOWN_FAST = 1
    DOWN_SLOW = 2
    STABLE    = 3
    UP_SLOW   = 4
    UP_FAST   = 5

Some earlier/other LibreLinkUp clients expose seven; we stay faithful to
what pylibrelinkup actually returns. This module has no dependency on
``pylibrelinkup`` itself so frontends can import it without pulling
the LLU client into their process.
"""

from __future__ import annotations

TREND_ARROWS: dict[int, str] = {
    1: "↓",
    2: "↘",
    3: "→",
    4: "↗",
    5: "↑",
}

TREND_NAMES: dict[int, str] = {
    1: "falling_fast",
    2: "falling",
    3: "stable",
    4: "rising",
    5: "rising_fast",
}


def _coerce(code: object) -> int | None:
    """Accept both the ``Trend`` IntEnum and a raw int; return a plain int."""
    if code is None:
        return None
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def trend_symbol(code: object) -> str:
    c = _coerce(code)
    return TREND_ARROWS.get(c, "") if c is not None else ""


def trend_name(code: object) -> str:
    c = _coerce(code)
    return TREND_NAMES.get(c, "unknown") if c is not None else "unknown"
