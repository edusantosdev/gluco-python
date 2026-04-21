#!/usr/bin/env python3
"""One-shot helper: stash LibreLinkUp credentials in macOS Keychain.

Run once before enabling the LaunchAgent::

    .venv/bin/python scripts/set_credentials.py

On success, the daemon can load ``LIBRELINKUP_EMAIL`` / ``_PASSWORD`` from
the Keychain on every boot, and ``.env`` stops being required (though it
still works as a fallback for dev or headless use).

The script never prints the password back. Use ``--clear`` to remove the
stored entries.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from glucose_common.credentials import (  # noqa: E402
    KEYCHAIN_SERVICE,
    clear_credentials,
    load_credentials,
    save_credentials,
)


def _prompt_credentials() -> tuple[str, str]:
    email = input("LibreLinkUp email: ").strip()
    if not email:
        raise SystemExit("aborted: empty email")
    password = getpass.getpass("LibreLinkUp password: ")
    if not password:
        raise SystemExit("aborted: empty password")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("aborted: passwords do not match")
    return email, password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clear",
        action="store_true",
        help="remove any stored credentials instead of setting them",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="show which source the daemon would use right now",
    )
    args = parser.parse_args()

    if args.show:
        creds = load_credentials(env_email=None, env_password=None)
        if creds is None:
            print("No credentials in Keychain. Daemon will use .env if present.")
            return 0
        # Never print the password back.
        print(f"Keychain: service='{KEYCHAIN_SERVICE}', email='{creds.email}'")
        return 0

    if args.clear:
        clear_credentials()
        print(f"Cleared any credentials stored under '{KEYCHAIN_SERVICE}'.")
        return 0

    email, password = _prompt_credentials()
    try:
        save_credentials(email, password)
    except Exception as e:  # noqa: BLE001
        print(f"Could not save to Keychain: {e}", file=sys.stderr)
        return 1
    print(
        f"Saved to Keychain as service='{KEYCHAIN_SERVICE}'. "
        "You can delete LIBRELINKUP_EMAIL/_PASSWORD from .env now."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
