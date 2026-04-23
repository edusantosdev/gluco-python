"""LibreView glucose-unit code → human-readable label.

The API encodes the unit as an integer on each reading. There is no
official upstream mapping in ``pylibrelinkup``; observed values are:

    1 → mg/dL  (default in US/JP, also DE/FR sometimes)
    0 → mmol/L (typical EU)

If a future region returns something else, ``glucose_unit_label`` returns
a fallback string so callers don't have to special-case ``None``.
"""

from __future__ import annotations

_UNIT_LABELS: dict[int, str] = {
    0: "mmol/L",
    1: "mg/dL",
}


def glucose_unit_label(code: object) -> str:
    if code is None:
        return "mg/dL"
    try:
        return _UNIT_LABELS.get(int(code), f"unit#{int(code)}")
    except (TypeError, ValueError):
        return "mg/dL"
