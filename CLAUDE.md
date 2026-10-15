# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,build]'

# Run tests
.venv/bin/python -m pytest tests/

# Run a single test file
.venv/bin/python -m pytest tests/test_backoff.py

# Lint
.venv/bin/python -m ruff check .

# Build .app bundle
scripts/build_portable_app.sh
open dist/Glucobar.app

# Run daemon only (dev)
.venv/bin/python -m daemon   # listens on 127.0.0.1:8765

# Store credentials in Keychain
.venv/bin/python scripts/set_credentials.py

# Test auth + region before running
.venv/bin/python scripts/phase0_probe.py
```

## Architecture

Glucobar is a two-process macOS menubar app with a split across a localhost HTTP boundary:

```
macos_frontend/  (rumps + AppKit UI)
  ├── app.py              — menubar UI, NSPanel modals, polling loop (30s)
  ├── daemon_supervisor.py — spawns/supervises daemon child process
  ├── api_client.py       — HTTP client hitting daemon endpoints
  └── graph_view.py       — 12-hour glucose trend graph rendering

daemon/  (FastAPI HTTP server, 127.0.0.1:8765)
  ├── main.py             — FastAPI route definitions
  ├── service.py          — polling loop (60s), LibreView calls, state transitions
  ├── state.py            — DaemonState + Status enum (STARTING/OK/STALE/NO_NETWORK/AUTH_FAILED/etc.)
  ├── config.py           — settings from env vars / .env file
  ├── backoff.py          — ExponentialBackoff (separate instances for errors vs. rate limits)
  ├── session_store.py    — persists LibreView session token to disk
  ├── single_instance.py  — fcntl lock at ~/.glucobar/daemon.lock
  ├── parent_watch.py     — pipe-based parent-death detection (daemon exits if frontend dies)
  ├── libreview_timeouts.py — adds the HTTP timeout pylibrelinkup omits
  └── local_guard.py      — rejects browser-originated requests (Host/Origin/Sec-Fetch-Site)

glucose_common/  (shared by both processes)
  ├── credentials.py      — macOS Keychain read/write (service="glucobar")
  ├── regions.py          — LibreView region → base URL mapping
  ├── trend.py            — trend arrow/name helpers
  └── units.py            — mmol/L ↔ mg/dL conversion
```

### Process lifecycle

1. Frontend reads Keychain (one macOS dialog), then spawns daemon with credentials passed via `GLUCOBAR_LIBREVIEW_EMAIL` / `GLUCOBAR_LIBREVIEW_PASSWORD` env vars — the daemon never touches Keychain itself.
2. If a daemon is already reachable on :8765, the frontend attaches instead of spawning.
3. Daemon exit code **78** (`EX_CONFIG`) means missing credentials — supervisor stops respawning until the user saves credentials and requests a restart.
4. Parent-death pipe ensures the daemon exits cleanly even on frontend force-quit.

### Daemon HTTP API (localhost only, no auth)

Requests must use a loopback `Host` and carry no foreign `Origin` / cross-site `Sec-Fetch-Site` (see `daemon/local_guard.py`); tests using `TestClient` need `base_url="http://127.0.0.1:8765"`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Daemon status (ok / degraded / down) |
| `GET /glucose/current` | Latest reading + status + age |
| `GET /glucose/graph?hours=N` | Cached history (oldest first) |
| `GET /patient` | Patient name |
| `POST /auth/refresh` | Force re-auth + immediate poll |

### State machine (`daemon/state.py`)

`STARTING` → `OK` → `STALE` (reading >360s old)  
Any poll cycle can transition to: `NO_NETWORK`, `ERROR`, `AUTH_FAILED`, `RATE_LIMITED`, `WARMING_UP`, `TOS_REQUIRED`

### Key configuration (env vars / `.env`)

`LIBREVIEW_REGION`, `DAEMON_URL`, `LOG_LEVEL`, `ERROR_BACKOFF_*`, `RATELIMIT_BACKOFF_*`  
See `.env.example` for all options.
