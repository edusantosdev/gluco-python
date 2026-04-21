# Glucose Menubar App — Implementation Plan

A cross-platform personal glucose display, starting as a macOS menubar app and designed from day one to port cleanly to Linux (and potentially other systems) without rewriting the data layer.

---

## 1. Goals

The project exists to surface a single piece of information — current glucose value with trend arrow — in a persistent, always-visible, zero-interaction way. No phone unlock, no app switch, no scanning. Just a glance at the menubar.

Secondary goals that shape the design:

The app should run reliably for months without intervention. It should survive macOS sleep/wake cycles, network blips, Abbott's periodic forced re-authentication, and sensor warm-up periods without crashing or requiring restart. It should also port to Linux without a rewrite of anything except the UI layer, because the same glucose data is valuable on a workstation regardless of OS. Finally, the architecture should be clean enough that a future Swift rewrite of the macOS frontend is a weekend of work, not a rescue operation.

What this project is explicitly *not*: a medical device, a dosing aid, a replacement for the official Libre app, or a multi-user system. It is a personal utility.

---

## 2. Architecture

The design is a three-layer separation, and the layers talk to each other over stable, documented interfaces so any single layer can be swapped without touching the others.

```
┌─────────────────────────────────────────────────────┐
│ Layer 3: UI Frontend                                │
│                                                     │
│ macOS: Python + rumps (MVP)  →  Swift (later)       │
│ Linux: Python + pystray or GTK                      │
│                                                     │
│ Talks to Layer 2 over HTTP (localhost)              │
└────────────────────┬────────────────────────────────┘
                     │ GET /glucose/current
                     │ GET /glucose/graph
                     ▼
┌─────────────────────────────────────────────────────┐
│ Layer 2: Data Daemon                                │
│                                                     │
│ Python + FastAPI, runs as background service        │
│ Handles: auth, polling, caching, error recovery     │
│ Exposes: localhost REST API                         │
│                                                     │
│ Talks to Layer 1 as a Python import                 │
└────────────────────┬────────────────────────────────┘
                     │ client.latest(patient)
                     ▼
┌─────────────────────────────────────────────────────┐
│ Layer 1: LibreLinkUp Client                         │
│                                                     │
│ pylibrelinkup (+ local patches for Phase 4 helpers) │
│                                                     │
│ Talks to Abbott's LibreLinkUp servers over HTTPS    │
└─────────────────────────────────────────────────────┘
```

The reason for the daemon layer — which initially feels like overkill for a menubar app — is that it is the single point where all Abbott-facing complexity lives. Token caching, rate-limit backoff, region redirects, ToS re-acceptance, sensor-warmup handling: all of it happens in one Python process. Every UI frontend becomes a dumb consumer of a stable internal API. When Abbott breaks something (and they will), there is exactly one place to fix it.

The daemon runs on `localhost` only, no authentication, no TLS. It is a personal tool on a trusted machine, not a service. Binding to `127.0.0.1` is sufficient isolation.

---

## 3. Tech Stack

**Layer 1 — LibreLinkUp client:** `pylibrelinkup` (current version 0.10.0). Rationale: actively maintained Python package, already handles the tricky parts of Abbott's auth flow (privacy policy errors, ToU errors, `Account-Id` header hash). Known gaps will be patched locally until they land upstream — see section 9.

**Layer 2 — Data daemon:** Python 3.11+, FastAPI, Uvicorn. Rationale: FastAPI gives typed JSON responses and automatic OpenAPI docs for free, which makes frontend development easier. Uvicorn runs it. Both are standard, boring, well-documented choices.

**Layer 3 — macOS MVP frontend:** Python 3.11+, `rumps`. Rationale: fastest path to a working menubar item. Thin wrapper over PyObjC/`NSStatusItem`, so escape hatches exist if needed.

**Layer 3 — Linux frontend (later):** Python + `pystray` for the simplest path, or GTK bindings (`PyGObject`) for proper integration with GNOME/KDE tray behavior. Decision deferred until actually porting.

**Layer 3 — Eventual Swift rewrite (optional):** SwiftUI with `MenuBarExtra`. Rationale: native macOS integration, single `.app` bundle with no Python runtime dependency, access to future Apple-only features like Live Activities or Watch complications. Not needed for MVP.

**Packaging (later):** `py2app` for macOS `.app` bundles, `briefcase` as an alternative if py2app proves painful. For the Linux frontend, a `systemd --user` service for the daemon plus a regular binary for the UI.

**Development tooling:** `uv` for environment management (faster than pip/poetry, handles Python versions), `ruff` for linting and formatting, `pytest` for tests. Standard modern Python stack.

---

## 4. Phased Roadmap

The work is organized so that each phase produces something usable. The daemon boundary is introduced in Phase 2, not Phase 1, because the MVP's job is to validate the product works at all — separation of concerns can wait one phase.

### Phase 0 — Verify the API works (30–60 min)

Create `pyproject.toml`, `.env.example`, `.gitignore`, and `scripts/phase0_probe.py` — a small script that loads `.env`, calls `PyLibreLinkUp.authenticate()` → `get_patients()` → `latest(patient)`, and prints the result as JSON. The goal is to confirm the API works for your account, in your region, before investing in any architecture. If this phase reveals a blocker (region detection failing, auth loop, ToS-required, whatever), the whole plan gets re-evaluated — and you've spent an hour rather than a weekend finding out.

### Phase 1 — Monolithic MVP (one evening)

Single Python process. `rumps` app imports `pylibrelinkup` directly, polls every 60 seconds in a `@rumps.timer` callback, updates the menubar title with value + trend arrow. No daemon, no HTTP, no separation. Credentials hardcoded in a config file. The goal is a working glucose display on your menubar by end of day. See section 6 for the full code.

### Phase 2 — Extract the daemon (one weekend)

Split the monolith into the three-layer architecture. Create the FastAPI daemon that owns the `pylibrelinkup` client and caches the current reading in memory. Rewrite the `rumps` app to poll the daemon over HTTP instead of calling `pylibrelinkup` directly. This is where the real architecture lands. Nothing visible changes from the user's perspective, but the codebase is now structured for porting.

### Phase 3 — Robustness (a few evenings)

Handle the failure modes that only reveal themselves over days of real use: sensor warm-up period (no data for 60 minutes after applying a new sensor), token expiry and re-auth, network loss and recovery, Abbott ToS re-acceptance, rate limiting. The daemon should persist its session to disk so restarts don't trigger fresh logins. See section 9 for the specific pylibrelinkup improvements that enable this.

### Phase 4 — macOS polish (optional, a weekend)

Launch-at-login via LaunchAgent plist. Preferences window for credentials. Notifications for hypo/hyper thresholds. Icon variations for low/in-range/high states. This is where the app graduates from "works for me" to "I actually rely on it."

### Phase 5 — Linux port (a few evenings)

Port the daemon unchanged (it's a standard FastAPI app, runs anywhere Python runs). Rewrite only the frontend using `pystray` or GTK. Most of the work is packaging and systemd integration, not code.

### Phase 6 — Swift rewrite (optional, later)

If the Python macOS frontend feels limiting, rewrite it in Swift against the same daemon HTTP API. Because the interface is documented and stable, this is genuinely a weekend of work — not a rescue project. Add Apple-native features at this point: Live Activities, Watch complication, Shortcuts integration.

---

## 5. Project Structure

```
glucose-display/
├── README.md
├── pyproject.toml                 # uv/pip project config
├── .env.example                   # credentials template
├── .gitignore
│
├── glucose_common/                # code shared by daemon + frontends
│   ├── __init__.py
│   └── trend.py                   # TREND_ARROWS, trend_name()
│
├── daemon/
│   ├── __init__.py
│   ├── config.py                  # pydantic-settings (env + defaults)
│   ├── main.py                    # FastAPI app + Uvicorn entry point
│   ├── service.py                 # GlucoseService: poller thread + cache
│   ├── state.py                   # Status enum + DaemonState dataclass
│   ├── session_store.py           # FileSessionStore for pylibrelinkup JWT
│   ├── backoff.py                 # exponential-backoff-with-jitter helper
│   ├── single_instance.py         # fcntl.flock lockfile guard
│   ├── logging_setup.py           # RotatingFileHandler wiring
│   └── models.py                  # Pydantic response schemas
│
├── macos_frontend/
│   ├── __init__.py
│   ├── app.py                     # rumps App subclass
│   ├── api_client.py              # thin httpx wrapper around daemon
│   └── resources/
│       └── icon.png
│
├── linux_frontend/                # Phase 5, empty for now
│   └── .gitkeep
│
├── swift_frontend/                # Phase 6, empty for now
│   └── .gitkeep
│
├── scripts/
│   ├── phase0_probe.py            # one-shot API smoke test (Phase 0)
│   ├── set_credentials.py         # writes creds to macOS Keychain (Phase 4)
│   ├── install_launchagent.sh     # macOS launch-at-login (Phase 4)
│   └── install_systemd.sh         # Linux equivalent (Phase 5)
│
└── tests/
    ├── conftest.py                # fake PyLibreLinkUp fixture
    ├── test_api_contract.py       # HTTP contract shape + status codes
    ├── test_service_poller.py     # cache updates, state transitions
    ├── test_retry_policy.py       # auth refresh, rate limit, backoff
    └── test_session_store.py      # disk persistence + permissions
```

The `daemon/`, `glucose_common/`, and `*_frontend/` directories are separate Python packages with their own `__init__.py`. The daemon and frontends communicate only over HTTP; `glucose_common` is the only code directly shared in-process (the trend-arrow map and similar trivia). Tests cover the daemon in isolation using a fake client — CI never hits Abbott's servers. The frontends are simple enough to test manually during development.

---

## 6. Phase 1 MVP Implementation

This section gives you something to run by end of day one. It's intentionally a monolith — the daemon split happens in Phase 2. What it is *not* intentionally a monolith on is the trend-arrow map: that lives in `glucose_common/trend.py` from day one, so Phase 2 is a move, not a duplication.

### `pyproject.toml`

```toml
[project]
name = "glucose-display"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "pylibrelinkup>=0.10.0,<0.11",
    "rumps>=0.4.0",
    "python-dotenv>=1.0.0",
]

[project.optional-dependencies]
dev = ["pytest", "ruff"]
daemon = ["fastapi>=0.110", "uvicorn>=0.29", "pydantic-settings>=2.0", "httpx>=0.27"]
```

The daemon's extra dependencies are behind an optional-dependency group so the Phase 1 install stays small.

### `.env` (gitignored)

```
LIBRELINKUP_EMAIL=you@example.com
LIBRELINKUP_PASSWORD=your_password
# Region must match the server your account lives on.
# Valid: US, EU, EU2, AE, AP, AU, CA, DE, FR, JP, LA, RU
LIBRELINKUP_REGION=US
POLL_INTERVAL_SECONDS=60
```

`LIBRELINKUP_REGION` is required in practice. `pylibrelinkup`'s `PyLibreLinkUp(email, password, api_url=APIUrl.US)` defaults to US, and the wrong region produces opaque auth errors rather than a helpful redirect.

### `glucose_common/trend.py`

`pylibrelinkup` exposes a 5-bucket `Trend` IntEnum (`DOWN_FAST=1, DOWN_SLOW=2, STABLE=3, UP_SLOW=4, UP_FAST=5`). Earlier plan drafts assumed 7 buckets — incorrect for this library.

```python
"""Shared trend-arrow mapping.

Replace with pylibrelinkup's own .symbol property once upstream adds it.
Kept dependency-free so frontends can import without pulling in LLU."""

TREND_ARROWS = {1: "↓", 2: "↘", 3: "→", 4: "↗", 5: "↑"}
TREND_NAMES = {
    1: "falling_fast", 2: "falling", 3: "stable",
    4: "rising", 5: "rising_fast",
}

def _coerce(code):
    if code is None: return None
    try: return int(code)
    except (TypeError, ValueError): return None

def trend_symbol(code) -> str:
    c = _coerce(code)
    return TREND_ARROWS.get(c, "") if c is not None else ""

def trend_name(code) -> str:
    c = _coerce(code)
    return TREND_NAMES.get(c, "unknown") if c is not None else "unknown"
```

The `_coerce` helper lets callers pass either the `Trend` IntEnum directly or a raw integer without caring which they have.

### `mvp.py`

```python
"""Phase 1 MVP — monolithic menubar glucose display."""

import os
import logging
from dotenv import load_dotenv
import rumps
from pylibrelinkup import (
    APIUrl,
    AuthenticationError,
    LLUAPIRateLimitError,
    PrivacyPolicyError,
    PyLibreLinkUp,
    TermsOfUseError,
)

from glucose_common.trend import trend_symbol

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("glucose")


class GlucoseApp(rumps.App):
    def __init__(self, email: str, password: str, poll_interval: int):
        super().__init__("···")  # loading placeholder
        self.poll_interval = poll_interval
        self.menu = ["Refresh now", None, "Last updated: never"]

        self.client = PyLibreLinkUp(email=email, password=password)
        self._authenticate()
        self.patient = self.client.get_patients()[0]

        # Explicit Timer instance, not @rumps.timer — avoids mutating the
        # decorated callback's interval attribute, which is an internal detail.
        self._timer = rumps.Timer(self.update, poll_interval)
        self._timer.start()
        self.update(None)  # initial fetch so user doesn't wait a full interval

    def _authenticate(self):
        try:
            self.client.authenticate()
            log.info("Authenticated with LibreLinkUp")
        except (TermsOfUseError, PrivacyPolicyError):
            log.error("ToS/Privacy re-accept required in the official Libre app")
            self.title = "⚠ ToS"
            raise
        except AuthenticationError as e:
            log.error("Auth failed: %s", e)
            self.title = "⚠ auth"
            raise

    def update(self, _):
        try:
            reading = self.client.latest(self.patient)
            self.title = f"{int(reading.value)} {trend_symbol(reading.trend)}"
            self.menu["Last updated: never"].title = (
                f"Last updated: {reading.timestamp.strftime('%H:%M')}"
            )
        except LLUAPIRateLimitError:
            log.warning("Rate limited")
            self.title = "⚠ rate"
        except (TermsOfUseError, PrivacyPolicyError):
            log.warning("ToS/Privacy re-accept required")
            self.title = "⚠ ToS"
        except AuthenticationError:
            log.warning("Auth lost; Phase 3 will auto-refresh")
            self.title = "⚠ auth"
        except Exception as e:
            log.warning("Poll failed: %s", e)
            self.title = "⚠"

    @rumps.clicked("Refresh now")
    def refresh_now(self, _):
        self.update(None)


if __name__ == "__main__":
    load_dotenv()
    app = GlucoseApp(
        email=os.environ["LIBRELINKUP_EMAIL"],
        password=os.environ["LIBRELINKUP_PASSWORD"],
        poll_interval=int(os.environ.get("POLL_INTERVAL_SECONDS", 60)),
    )
    app.run()
```

Run with `python mvp.py`. Glucose value and arrow appear in your menubar. That's the MVP.

### What this MVP intentionally doesn't do

No credential UI — edit the `.env` file. No error recovery beyond glyph-based surfacing of known failure classes. No sensor-warmup handling — if the sensor is warming up, you'll see `⚠` until it's ready. No persistence — every restart re-authenticates. No `.app` bundle — it runs from the terminal.

All of these are addressed in later phases, and none are blockers to daily use.

---

## 7. Phase 2 — The Daemon Split

This is where the architecture becomes real. The MVP's single file becomes three components.

### Daemon responsibilities

The daemon owns the `pylibrelinkup` client, runs the polling loop, caches the latest reading, and exposes a small HTTP API. It has no UI, starts on login, and runs for months.

### Threading model

`pylibrelinkup` is a synchronous library — network calls block. The poller therefore runs in a dedicated **background thread**, not as an asyncio task. The FastAPI route handlers stay async but do non-blocking reads from an in-memory state object guarded by a `threading.Lock`. This is simpler than sprinkling `asyncio.to_thread(...)` through the service, and it cleanly decouples poll cadence from request latency.

### Daemon status enum

Every failure mode the user cares about is surfaced as a single discriminated state, so frontends never need to reverse-engineer status from HTTP codes or error strings:

```python
# daemon/state.py
from enum import Enum

class Status(str, Enum):
    STARTING       = "starting"        # daemon up, no reading yet
    OK             = "ok"              # fresh reading
    STALE          = "stale"           # last reading older than threshold
    WARMING_UP     = "warming_up"      # sensor in 60-min warm-up
    SENSOR_ENDED   = "sensor_ended"    # sensor expired or not attached
    NO_NETWORK     = "no_network"      # connection failures, backing off
    AUTH_FAILED    = "auth_failed"     # credentials rejected
    TOS_REQUIRED   = "tos_required"    # user must accept in Libre app
    RATE_LIMITED   = "rate_limited"    # honoring retry_after
```

### HTTP API contract

This is the interface every frontend depends on. Keep it stable.

```
GET  /health
  → { "status": "ok" | "degraded" | "down",
      "daemon_status": "<Status>",
      "uptime_seconds": 12345,
      "last_error": "string | null" }

GET  /glucose/current
  → { "status": "<Status>",
      "value": 124,                           # omitted if no reading
      "unit": "mg/dL",
      "trend": "stable",
      "trend_symbol": "→",
      "sensor_timestamp": "2026-04-21T14:32:00Z",
      "fetched_at":       "2026-04-21T14:32:05Z",
      "reading_age_seconds": 47,              # sensor_timestamp → now
      "fetch_age_seconds":   42 }             # fetched_at → now
  # 200 with status set when in any non-ok state.
  # 503 only when the daemon genuinely cannot serve (pre-startup).

GET  /glucose/graph?hours=12
  → [ { "value": 124, "timestamp": "..." }, ... ]

GET  /patient
  → { "first_name": "...",
      "sensor_active": true,
      "sensor_expires_at": "2026-05-05T10:00:00Z" }

POST /auth/refresh
  → forces re-authentication; returns updated /health
```

Two ages are exposed, not one. `reading_age_seconds` measures staleness from the sensor's point of view (what the user cares about), while `fetch_age_seconds` measures staleness of our own poll (what diagnostics care about). These diverge whenever Abbott hasn't pushed a new reading since we last fetched.

Non-`ok` states return HTTP 200, not 4xx/5xx — they're legitimate operational states, not errors. Only genuine daemon failure (pre-startup, cache empty) returns 503.

### `daemon/service.py` (sketch)

```python
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pylibrelinkup import PyLibreLinkUp
from pylibrelinkup.exceptions import (
    AuthenticationError, TermsOfUseError, PrivacyPolicyError,
    LLUAPIRateLimitError,
)

from .state import Status
from .backoff import ExponentialBackoff

log = logging.getLogger(__name__)


@dataclass
class DaemonState:
    status: Status = Status.STARTING
    reading: object | None = None
    fetched_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class GlucoseService:
    def __init__(self, email: str, password: str, poll_interval: int, session_store):
        self.client = PyLibreLinkUp(
            email=email, password=password, session_store=session_store
        )
        self.poll_interval = poll_interval
        self.patient = None
        self.state = DaemonState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff = ExponentialBackoff(base=30, cap=300, jitter=0.2)

    def start(self):
        try:
            self.client.authenticate()
            self.patient = self.client.get_patients()[0]
        except (TermsOfUseError, PrivacyPolicyError):
            self._set_status(Status.TOS_REQUIRED)
        except AuthenticationError as e:
            self._set_status(Status.AUTH_FAILED, str(e))
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def snapshot(self) -> DaemonState:
        with self._lock:
            return DaemonState(**self.state.__dict__)  # shallow copy

    # --- internals ---

    def _set_status(self, status: Status, error: str | None = None):
        with self._lock:
            self.state.status = status
            self.state.last_error = error

    def _poll_loop(self):
        while not self._stop.is_set():
            sleep_for = self.poll_interval
            if self.state.status == Status.TOS_REQUIRED:
                sleep_for = 300  # don't hammer while user takes action
            else:
                try:
                    self._poll_once()
                    self._backoff.reset()
                except LLUAPIRateLimitError as e:
                    self._set_status(Status.RATE_LIMITED)
                    sleep_for = getattr(e, "retry_after", 60)
                except (TermsOfUseError, PrivacyPolicyError):
                    self._set_status(Status.TOS_REQUIRED)
                except AuthenticationError:
                    # One silent re-auth attempt; if it fails, mark broken.
                    try:
                        self.client.authenticate()
                        continue
                    except Exception as e:
                        self._set_status(Status.AUTH_FAILED, str(e))
                except Exception as e:
                    log.warning("Poll failed: %s", e)
                    self._set_status(Status.NO_NETWORK, str(e))
                    sleep_for = self._backoff.next()
            self._stop.wait(sleep_for)

    def _poll_once(self):
        reading = self.client.latest(self.patient)
        with self._lock:
            self.state.reading = reading
            self.state.fetched_at = datetime.now(timezone.utc)
            self.state.status = Status.OK
            self.state.last_error = None
            self.state.consecutive_failures = 0
```

### `daemon/main.py` (sketch)

```python
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException

from glucose_common.trend import trend_symbol, trend_name
from .config import settings
from .logging_setup import configure_logging
from .session_store import FileSessionStore
from .single_instance import acquire_lock
from .service import GlucoseService
from .state import Status

configure_logging(settings.log_dir)
acquire_lock(settings.lock_path)  # exits if another daemon holds it

service = GlucoseService(
    email=settings.email,
    password=settings.password.get_secret_value(),
    poll_interval=settings.poll_interval,
    session_store=FileSessionStore(settings.session_path),
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    service.start()
    try:
        yield
    finally:
        service.stop()

app = FastAPI(lifespan=lifespan, title="glucose-daemon")

def _http_status_summary(s: Status) -> str:
    if s == Status.OK:
        return "ok"
    if s in (Status.STARTING, Status.WARMING_UP, Status.STALE,
             Status.RATE_LIMITED, Status.NO_NETWORK):
        return "degraded"
    return "down"  # AUTH_FAILED, TOS_REQUIRED, SENSOR_ENDED

@app.get("/health")
async def health():
    s = service.snapshot()
    uptime = (datetime.now(timezone.utc) - s.started_at).total_seconds()
    return {
        "status": _http_status_summary(s.status),
        "daemon_status": s.status,
        "uptime_seconds": int(uptime),
        "last_error": s.last_error,
    }

@app.get("/glucose/current")
async def current():
    s = service.snapshot()
    if s.reading is None and s.status == Status.STARTING:
        raise HTTPException(503, "Daemon starting; no reading yet")
    now = datetime.now(timezone.utc)
    body = {"status": s.status, "unit": str(s.reading.glucose_units) if s.reading else settings.unit}
    if s.reading is not None:
        r = s.reading
        body.update(
            value=r.value,
            trend=trend_name(r.trend),
            trend_symbol=trend_symbol(r.trend),
            sensor_timestamp=r.timestamp.isoformat(),
            fetched_at=s.fetched_at.isoformat(),
            reading_age_seconds=int((now - r.timestamp).total_seconds()),
            fetch_age_seconds=int((now - s.fetched_at).total_seconds()),
        )
    return body
```

Run with `uvicorn daemon.main:app --host 127.0.0.1 --port 8765`.

### `macos_frontend/app.py` (sketch)

```python
import rumps
import httpx

STATUS_GLYPH = {
    "starting": "···",
    "warming_up": "◴",
    "sensor_ended": "⚠ sensor",
    "no_network": "⚠ net",
    "auth_failed": "⚠ auth",
    "tos_required": "⚠ ToS",
    "rate_limited": "⚠ rate",
}

class GlucoseApp(rumps.App):
    def __init__(self):
        super().__init__("···")
        self.client = httpx.Client(base_url="http://127.0.0.1:8765", timeout=5)
        self._timer = rumps.Timer(self.update, 30)
        self._timer.start()

    def update(self, _):
        try:
            r = self.client.get("/glucose/current")
            r.raise_for_status()
            data = r.json()
            status = data["status"]
            if status == "ok":
                self.title = f"{int(data['value'])} {data['trend_symbol']}"
            elif status == "stale" and "value" in data:
                self.title = f"{int(data['value'])} {data['trend_symbol']}*"
            else:
                self.title = STATUS_GLYPH.get(status, "⚠")
        except httpx.HTTPError:
            self.title = "⚠ daemon"

if __name__ == "__main__":
    GlucoseApp().run()
```

The frontend is trivial — no credentials, no `pylibrelinkup` import, no knowledge of Abbott's API. All decisions about state live in the daemon; the frontend only maps status strings to glyphs. That's the whole point.

---

## 8. Phase 3 — Robustness

This is where a toy becomes something you can actually rely on. The failure modes to handle, and the exact mechanisms that handle them:

**Sensor warm-up.** After applying a new sensor, there is a 60-minute warm-up period with no readings. Detection: either `pylibrelinkup` raises a warm-up-specific exception (once upstream adds one) or the current call returns a reading whose timestamp predates the recorded sensor-applied time. The daemon transitions to `WARMING_UP`, surfaces the estimated ready time in `/patient`, and the frontend shows `◴` rather than `⚠`.

**Token expiry.** LibreLinkUp tokens last roughly an hour. The daemon catches `AuthenticationError` in the poll loop, calls `client.authenticate()` exactly once, and retries the poll on the next tick. If the second attempt also fails, it transitions to `AUTH_FAILED` rather than retrying in a tight loop. `FileSessionStore` (below) persists the JWT so a daemon restart doesn't force a fresh login.

**Network loss.** When the machine wakes from sleep or the network drops, the first poll after the gap will fail. `daemon/backoff.py` provides an `ExponentialBackoff(base=30, cap=300, jitter=0.2)` that yields 30, 60, 120, 240, 300 seconds (±20% jitter, reset-on-success). Jitter matters because on laptop wake-from-sleep many background services reconnect at once; a deterministic 30-second delay would thundering-herd.

**Rate limiting.** `pylibrelinkup` raises `LLUAPIRateLimitError` with a `retry_after` field. The poll loop sleeps for that exact duration and sets `RATE_LIMITED` in the meantime.

**Abbott ToS re-acceptance.** `pylibrelinkup` raises `TermsOfUseError` or `PrivacyPolicyError`. The daemon cannot solve this automatically — the user must open the official Libre app on their phone and accept. While in `TOS_REQUIRED`, the poller slows to once every 5 minutes (hammering Abbott with rejected requests is pointless and burns rate-limit budget). The frontend shows "action required on phone" in the dropdown.

**Daemon crashes.** On macOS, a LaunchAgent with `KeepAlive` set to true will auto-restart the daemon if it exits. On Linux, `systemd --user` with `Restart=on-failure` does the same. Critically, we also need to make sure *only one* daemon runs.

**Single-instance guard.** `daemon/single_instance.py` opens `~/.glucose-display/daemon.lock` and calls `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. If another daemon already holds it, `acquire_lock()` logs and calls `sys.exit(1)`. Without this, a misfiring LaunchAgent or a manual double-launch will produce two processes racing against the same session file and tripping Abbott's rate limiter.

**Graceful shutdown.** FastAPI's `lifespan` `finally` block calls `service.stop()`, which sets a `threading.Event` and `join`s the poller thread with a 5-second timeout. SIGTERM from launchd/systemd now exits cleanly rather than leaving orphaned HTTP connections mid-flight.

**Structured logging.** `daemon/logging_setup.py` installs a `RotatingFileHandler('~/.glucose-display/logs/daemon.log', maxBytes=1_000_000, backupCount=5)` plus a console handler. Default level `INFO`, overridable with `LOG_LEVEL=DEBUG`. A daemon that runs for months without logs is un-debuggable; a daemon that grows an unbounded log file is a different kind of broken.

### `FileSessionStore` specification

`pylibrelinkup` currently holds its JWT in memory only. Until session persistence lands upstream, the daemon patches it in:

```python
# daemon/session_store.py
import json, os, stat
from pathlib import Path
from datetime import datetime, timezone

class FileSessionStore:
    """Persists the pylibrelinkup JWT and expiry to disk.

    Wired into the client via either (a) the upstream session_store
    constructor argument once it exists, or (b) a monkey-patch of
    PyLibreLinkUp._after_authenticate in daemon/patches.py."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        exp = data.get("expires_at")
        if exp and datetime.fromisoformat(exp) <= datetime.now(timezone.utc):
            return None  # stale
        return data

    def save(self, token: str, expires_at: datetime) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "token": token,
            "expires_at": expires_at.isoformat(),
        }))
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
```

The 0600 file mode matters: other users on the machine (if any) should not be able to read the JWT. The atomic rename via `.tmp` prevents torn writes if the daemon is killed mid-save.

All of the above live in `daemon/` and are unit-testable without hitting Abbott's API. `tests/test_retry_policy.py` uses a fake client that raises specific exception types in specified orders and asserts the service transitions correctly through the `Status` enum.

---

## 9. Required pylibrelinkup Improvements

The separate `pylibrelinkup-improvement-plan.md` document covers six phases of library improvements. For this project specifically, only a subset matters:

**Must-have for this project** (implement locally as patches if upstream is slow):

Session persistence to disk (Phase 2 of the library plan) — essential so the daemon doesn't re-login on every restart. Implement as `FileSessionStore` and pass to the client constructor.

Trend arrow enum with `.symbol` property (Phase 4 of the library plan) — would let us delete `glucose_common/trend.py` entirely. `pylibrelinkup` already exposes the 5-value `Trend` IntEnum on `reading.trend`, so this is purely a convenience improvement.

**Nice-to-have:**

Region auto-detection (Phase 3 of the library plan) — one-time setup convenience. Not critical if you already know your region.

`poll()` utility (Phase 5 of the library plan) — the daemon has its own polling loop so this isn't strictly needed, but if upstream ships it, refactor to use it.

**Not needed:**

Async client (Phase 1 of the library plan). FastAPI tolerates sync `pylibrelinkup` calls inside async route handlers via `asyncio.to_thread()`, which is fine at this scale. If the daemon starts handling many concurrent requests, revisit.

If you end up patching `pylibrelinkup` locally, keep the patches in a separate file (`daemon/patches.py` with monkey-patches, or a fork on a branch). Upstreaming them is a non-goal — the library maintainer has their own roadmap.

---

## 10. Deployment

**macOS development:** run daemon and frontend in two terminals, iterate freely.

**macOS daily use (Phase 4):**

A LaunchAgent plist at `~/Library/LaunchAgents/com.glucose-display.daemon.plist` starts the daemon on login with `KeepAlive` set to true. The frontend is a separate LaunchAgent that runs the rumps app. Keeping them as separate agents means a frontend crash doesn't kill the daemon, and launchd's `ThrottleInterval` (default 10s) prevents a crash-loop from burning CPU.

Credentials live in macOS Keychain, not `.env`, once the app is packaged. The `keyring` Python library handles this cross-platform. `scripts/set_credentials.py` is a one-shot helper that prompts for email/password and writes them to Keychain; the daemon's `config.py` reads Keychain first and falls back to `.env` only if the Keychain entry is absent, so development and production use the same code path.

When the frontend is eventually bundled as a `.app` (py2app/briefcase), its `Info.plist` needs `LSUIElement=1` so it shows only a menubar item and no dock icon. Not relevant when running `python macos_frontend/app.py` from the terminal during development, but a silent gotcha at packaging time.

**macOS distribution (Phase 6, only if sharing with others):**

Code signing requires an Apple Developer account ($99/year). Notarization is required for modern macOS to run the app without Gatekeeper warnings. If you don't sign and notarize, recipients can still run it, but they need to right-click → Open the first time.

For personal use only, skip all of this. Run unsigned from your own machine.

**Linux (Phase 5):**

`systemd --user` unit for the daemon, standard desktop-file for the frontend. Both live in `~/.config/`. Distro packaging (deb/rpm/flatpak) is a later concern.

---

## 11. Open Questions

Things that don't need answers now but will come up:

**Multiple sensors/patients.** The MVP assumes one patient, one sensor. If you have followers in LibreLinkUp (family members' data), the daemon would need a patient-selection endpoint and the frontend a picker. Probably out of scope forever for personal use, but worth noting.

**Historical data and graphs.** A menubar app naturally wants a dropdown with the last few hours. `pylibrelinkup.graph()` returns 12 hours of 1-minute readings, which is plenty. Question: render in the rumps menu (ASCII sparkline) or pop out a small SwiftUI/GTK window? Deferred until you want it.

**Offline behavior.** If the machine has no network for an hour, what should the menubar show? Last known value with a staleness indicator is probably right, but "last known value from 3 days ago" is actively misleading. Define a staleness cutoff (15 minutes?) and show "no data" beyond it.

**Units.** This plan assumes mg/dL throughout. If you're in a mmol/L region, add a `UNITS` env var and convert in the daemon response. Don't do unit conversion in the frontend — keep it a dumb display.

**Alarms.** The Libre app's alarm system is local-only and tied to the phone. If you want desktop alarms for hypos, they have to be implemented here. Basic threshold alarms in the daemon (fire a macOS notification via `pync` or similar) are easy; matching Abbott's sophistication isn't.

---

## 12. Summary

Phase 1 is an evening's work and gets you glucose on your menubar. Phase 2 is the weekend that earns portability. Phases 3–4 transform it into something reliable. Phases 5–6 are optional futures.

The architecture's core insight: **put the Abbott-facing complexity in one Python daemon with a stable local HTTP interface, and every UI frontend in every language becomes a dumb consumer of that interface.** You pay a small upfront cost in Phase 2 and save enormous cost on every subsequent port, feature, and Abbott breakage.

Start with Phase 0. Confirm the API works for your account before building anything else.
