"""Phase 0 — Verify the LibreView API works for your account.

Run this BEFORE doing anything else in the project. If it prints a glucose
value, the rest of the architecture is worth building. If it fails, read
the error and fix that first (region wrong, ToS not accepted, etc.).

Reads credentials from the macOS Keychain (the only place Glucobar
stores them). If none are present, seed them first::

    .venv/bin/python scripts/set_credentials.py
    .venv/bin/python scripts/phase0_probe.py
    .venv/bin/python scripts/phase0_probe.py --region EU   # if outside the US
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402
from pylibrelinkup import (  # noqa: E402
    AuthenticationError,
    LLUAPIRateLimitError,
    PrivacyPolicyError,
    PyLibreLinkUp,
    TermsOfUseError,
)

from glucose_common.credentials import load_credentials  # noqa: E402
from glucose_common.regions import (  # noqa: E402
    guess_libreview_region,
    resolve_libreview_region,
)


def main() -> int:
    # ``.env`` only carries non-secret settings now (region, thresholds,
    # etc.); loading it keeps ``LIBREVIEW_REGION`` honored without
    # pulling credentials from it.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region",
        default=os.environ.get("LIBREVIEW_REGION", guess_libreview_region()),
        help="LibreView region (US, EU, EU2, AE, AP, AU, CA, DE, FR, JP, LA, RU)",
    )
    args = parser.parse_args()

    creds = load_credentials()
    if creds is None:
        raise SystemExit(
            "No LibreView credentials in the macOS Keychain. "
            "Run `.venv/bin/python scripts/set_credentials.py` first."
        )

    region, region_hint = resolve_libreview_region(args.region)
    print(f"Region: {region.name} ({region.value})", file=sys.stderr)
    if region_hint:
        print(f"Note: {region_hint}", file=sys.stderr)

    client = PyLibreLinkUp(email=creds.email, password=creds.password, api_url=region)
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
            "Authenticated OK but no patients are shared with this LibreView "
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
