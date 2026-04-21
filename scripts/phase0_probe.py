"""Phase 0 — Verify the LibreLinkUp API works for your account.

Run this BEFORE doing anything else in the project. If it prints a glucose
value, the rest of the architecture is worth building. If it fails, read
the error and fix that first (region wrong, ToS not accepted, etc.).

Usage:
    cp .env.example .env          # then edit with your credentials
    .venv/bin/python scripts/phase0_probe.py
    .venv/bin/python scripts/phase0_probe.py --region EU   # if outside the US
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow `python scripts/phase0_probe.py` — repo root must be on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
from pylibrelinkup import (
    AuthenticationError,
    LLUAPIRateLimitError,
    PrivacyPolicyError,
    PyLibreLinkUp,
    TermsOfUseError,
)

from glucose_common.regions import resolve_librelinkup_region


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region",
        default=os.environ.get("LIBRELINKUP_REGION", "US"),
        help="LibreLinkUp region (US, EU, EU2, AE, AP, AU, CA, DE, FR, JP, LA, RU)",
    )
    args = parser.parse_args()

    email = os.environ.get("LIBRELINKUP_EMAIL")
    password = os.environ.get("LIBRELINKUP_PASSWORD")
    if not email or not password:
        raise SystemExit(
            "LIBRELINKUP_EMAIL / LIBRELINKUP_PASSWORD not set. "
            "Copy .env.example to .env and fill it in."
        )

    region, region_hint = resolve_librelinkup_region(args.region)
    print(f"Region: {region.name} ({region.value})", file=sys.stderr)
    if region_hint:
        print(f"Note: {region_hint}", file=sys.stderr)

    client = PyLibreLinkUp(email=email, password=password, api_url=region)
    try:
        client.authenticate()
    except TermsOfUseError:
        print(
            "Terms of Use re-acceptance required. Open the official Libre app "
            "on your phone and accept the prompts, then re-run.",
            file=sys.stderr,
        )
        return 2
    except PrivacyPolicyError:
        print(
            "Privacy Policy re-acceptance required. Open the official Libre app "
            "on your phone and accept the prompts, then re-run.",
            file=sys.stderr,
        )
        return 2
    except AuthenticationError as e:
        print(f"Authentication failed: {e}", file=sys.stderr)
        return 2
    except LLUAPIRateLimitError as e:
        print(f"Rate limited; retry after {getattr(e, 'retry_after', '?')}s", file=sys.stderr)
        return 3

    patients = client.get_patients()
    if not patients:
        print(
            "Authenticated OK but no patients are shared with this LibreLinkUp "
            "account. In the official Libre app, make sure someone has invited "
            "you (or you are self-sharing).",
            file=sys.stderr,
        )
        return 4

    patient = patients[0]
    print(f"Patient: {patient.first_name} {patient.last_name}", file=sys.stderr)

    try:
        reading = client.latest(patient)
    except Exception as e:  # noqa: BLE001 — diagnostic script wants the full picture
        print(f"latest() failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 5

    payload = {
        "value": reading.value,
        "value_in_mg_per_dl": reading.value_in_mg_per_dl,
        "glucose_units": str(reading.glucose_units),
        "trend": int(reading.trend),
        "trend_name": reading.trend.name if hasattr(reading.trend, "name") else None,
        "timestamp": reading.timestamp.isoformat(),
        "factory_timestamp": reading.factory_timestamp.isoformat()
        if reading.factory_timestamp
        else None,
        "is_high": reading.is_high,
        "is_low": reading.is_low,
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
