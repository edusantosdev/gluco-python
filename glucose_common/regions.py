"""Map ``LIBREVIEW_REGION`` env values to ``pylibrelinkup.APIUrl``.

LibreView exposes named clusters (``US``, ``EU``, ``DE``, …), not every ISO
country code. Users often set their country (e.g. ``PT``) — we translate
those to the correct cluster and optionally emit a hint for logs.
"""

from __future__ import annotations

import os
import re

from pylibrelinkup import APIUrl

# ISO 3166-1 alpha-2 (and a few colloquial codes) → ``APIUrl`` member name.
# Only list countries that do *not* have their own ``api-xx.libreview.io``
# host in the enum (``DE``, ``FR``, ``JP``, … must stay as themselves).
_REGION_ALIASES: dict[str, str] = {
    # Iberia & UK → EU cluster
    "PT": "EU",
    "ES": "EU",
    "GB": "EU",
    "UK": "EU",
    # Other EU countries without a dedicated host
    "AT": "EU",
    "BE": "EU",
    "BG": "EU",
    "CH": "EU",
    "CY": "EU",
    "CZ": "EU",
    "DK": "EU",
    "EE": "EU",
    "FI": "EU",
    "GR": "EU",
    "HR": "EU",
    "HU": "EU",
    "IE": "EU",
    "IS": "EU",
    "IT": "EU",
    "LI": "EU",
    "LT": "EU",
    "LU": "EU",
    "LV": "EU",
    "MT": "EU",
    "NL": "EU",
    "NO": "EU",
    "PL": "EU",
    "RO": "EU",
    "SE": "EU",
    "SI": "EU",
    "SK": "EU",
}


def guess_libreview_region(default: str = "US") -> str:
    """Best-effort region token from locale env vars.

    Returns an ISO-like country code (for example ``PT``) when we can
    infer one from ``LC_ALL`` / ``LC_MESSAGES`` / ``LANG``; otherwise
    returns ``default``. The caller can then pass this to
    :func:`resolve_libreview_region` so aliases map to LibreView
    clusters (for example ``PT`` -> ``EU``).
    """
    pattern = re.compile(r"^[A-Za-z]{2}[_-]([A-Za-z]{2})")
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        match = pattern.match(raw)
        if match:
            return match.group(1).upper()
    return default


def resolve_libreview_region(raw: str) -> tuple[APIUrl, str | None]:
    """Return ``(api_url, hint)`` where ``hint`` is set when an alias was used."""
    key = raw.strip().upper()
    if not key:
        key = "US"
    mapped = _REGION_ALIASES.get(key, key)
    hint = f"{key} → {mapped} (LibreView cluster)" if mapped != key else None
    try:
        return APIUrl[mapped], hint
    except KeyError:
        valid_api = ", ".join(m.name for m in APIUrl)
        valid_alias = ", ".join(sorted(_REGION_ALIASES))
        raise ValueError(
            f"Unknown LIBREVIEW_REGION '{raw.strip()}'.\n"
            f"  Use a cluster name: {valid_api}\n"
            f"  Or a supported country code: {valid_alias}\n"
            f"  Portugal: set EU (or PT — we map PT to EU)."
        ) from None
