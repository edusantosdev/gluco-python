"""Persist the LibreLinkUp auth token across daemon restarts.

Rationale
---------
``pylibrelinkup.authenticate()`` hits Abbott's login endpoint on every
call. Running it on every daemon restart is:

1. Wasteful — LibreView tickets are valid for ~1 hour.
2. Risky — too many logins from one account can trip rate-limiting
   or account-lock heuristics.

So we save the JWT + the account id hash to a file in the state
directory, restore them on startup, and only re-authenticate on
``AuthenticationError`` or when the JWT is near expiry.

Expiry extraction
-----------------
LibreView tokens are JWTs; their payload carries a standard ``exp``
claim (unix seconds). We parse it passively (no signature verification —
we're not the API server, we just want the expiry for scheduling).
If parsing fails, we fall back to a conservative TTL set at save time.

Security notes
--------------
The token grants read access to the *connections* endpoints for this
LibreLinkUp account. We store it mode-0600 under ``state_dir``. This is
equivalent in sensitivity to the plaintext credentials in ``.env`` that
the daemon already reads; upgrading to Keychain via ``keyring`` is a
Phase 4 nicety.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Fallback TTL when the JWT doesn't carry an ``exp`` claim we can parse.
# Auth tickets historically last ~1 hour; 50 min leaves a comfortable buffer.
_FALLBACK_TTL_SECONDS = 50 * 60

# Refresh this many seconds before reported expiry to avoid 401-on-boundary.
_EXPIRY_SKEW_SECONDS = 60


@dataclass(frozen=True)
class Session:
    token: str
    account_id_hash: str
    expires_at: float  # unix epoch seconds

    @property
    def is_expired(self) -> bool:
        return time.time() >= (self.expires_at - _EXPIRY_SKEW_SECONDS)


def _decode_jwt_exp(token: str) -> float | None:
    """Extract the JWT ``exp`` claim without verifying the signature.

    Returns ``None`` if the token isn't a three-part JWT or the payload
    can't be decoded. We never trust this for auth decisions — it's
    purely a scheduling hint. The server still enforces real expiry.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded)
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — malformed token is non-fatal
        return None


class FileSessionStore:
    """JSON-on-disk session store with atomic writes and mode-0600 files."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Session | None:
        """Return a valid session, or ``None`` if missing/expired/corrupt."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            log.warning("Could not read session file %s: %s", self.path, e)
            return None
        try:
            data = json.loads(raw)
            session = Session(
                token=data["token"],
                account_id_hash=data["account_id_hash"],
                expires_at=float(data["expires_at"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.warning("Session file %s is corrupt (%s); ignoring", self.path, e)
            return None
        if session.is_expired:
            log.info("Stored session expired; will re-authenticate")
            return None
        return session

    def save(self, token: str, account_id_hash: str) -> Session:
        """Persist a session and return it. Uses JWT ``exp`` if available."""
        exp = _decode_jwt_exp(token)
        if exp is None:
            exp = time.time() + _FALLBACK_TTL_SECONDS
            log.debug("JWT exp not parseable; using %ds fallback TTL", _FALLBACK_TTL_SECONDS)
        session = Session(token=token, account_id_hash=account_id_hash, expires_at=exp)
        self._atomic_write(session)
        return session

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not remove session file %s: %s", self.path, e)

    # --- internals ---

    def _atomic_write(self, session: Session) -> None:
        """Write via tempfile + rename so a crash mid-write can't corrupt."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "token": session.token,
                "account_id_hash": session.account_id_hash,
                "expires_at": session.expires_at,
            }
        )
        # NamedTemporaryFile in the same dir so rename() is atomic on the
        # same filesystem — required for POSIX atomicity guarantees.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".session.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
