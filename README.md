# librelinkup-glucose-menubar-python

A personal macOS menubar app that displays your current FreeStyle Libre glucose
value via LibreLinkUp. Designed to port cleanly to Linux. See
[`glucose-menubar-implementation-plan.md`](./glucose-menubar-implementation-plan.md)
for the full plan and reasoning.

## Status

- **Phase 0** — API verification — ✅ done
- **Phase 1** — Monolithic MVP (`mvp.py`) — ✅ done
- **Phase 2** — Daemon split (FastAPI + thin frontend) — ✅ done
- **Phase 3** — Robustness (session persistence, backoff, log rotation, warm-up detection) — ✅ done
- **Phase 4** — macOS polish (LaunchAgent, Keychain, notifications) — pending
- **Phase 5** — Linux port — pending

## Setup (one-time)

```bash
# 1. Make a virtualenv with Python 3.11+
python3 -m venv .venv             # or: uv venv --python 3.12 .venv
.venv/bin/pip install pylibrelinkup rumps python-dotenv \
                     fastapi uvicorn pydantic-settings httpx

# 2. Copy and edit credentials
cp .env.example .env
$EDITOR .env                       # set email, password, region
```

`LIBRELINKUP_REGION` accepts cluster names (`US`, `EU`, `EU2`, `DE`, `FR`, `JP`,
`AU`, `CA`, `AE`, `AP`, `LA`, `RU`) or country codes that map to one of them
(e.g. `PT`, `ES`, `GB` → `EU`).

## Running

### Phase 1 — Single-file MVP

The simplest path: one process polls Abbott directly and updates the menubar.

```bash
.venv/bin/python mvp.py
```

### Phase 2 — Daemon + frontend (recommended)

The daemon owns all LibreLinkUp interaction; the menubar app is a thin
HTTP consumer of it. Two terminals:

```bash
# Terminal 1 — daemon
.venv/bin/python -m daemon
#  Serves http://127.0.0.1:8765
#  Writes lock + future session/logs to ~/.glucose-display/

# Terminal 2 — menubar frontend
.venv/bin/python -m macos_frontend
```

Single-command convenience (run both from one terminal):

```bash
.venv/bin/python -m daemon & .venv/bin/python -m macos_frontend
```

If you want `Ctrl+C` to also stop the background daemon:

```bash
(.venv/bin/python -m daemon &) ; DAEMON_PID=$! ; .venv/bin/python -m macos_frontend ; kill $DAEMON_PID 2>/dev/null
```

Probe the daemon by hand:

```bash
curl -s http://127.0.0.1:8765/health           | jq
curl -s http://127.0.0.1:8765/glucose/current  | jq
curl -s http://127.0.0.1:8765/patient          | jq
curl -s http://127.0.0.1:8765/glucose/graph    | jq
curl -s -X POST http://127.0.0.1:8765/auth/refresh | jq
```

OpenAPI / Swagger docs: <http://127.0.0.1:8765/docs>.

### Phase 0 — API smoke test

If anything looks off, re-verify Abbott itself is reachable for your account:

```bash
.venv/bin/python scripts/phase0_probe.py
```

## Menubar status glyphs

The frontend maps daemon status values to short titles:

| Status         | Title shown        |
|----------------|--------------------|
| `ok`           | `124 →`            |
| `stale`        | `124 →*`           |
| `starting`     | `···`              |
| `warming_up`   | `◴`                |
| `no_network`   | `⚠ net`            |
| `auth_failed`  | `⚠ auth`           |
| `tos_required` | `⚠ ToS`            |
| `rate_limited` | `⚠ rate`           |
| daemon down    | `⚠ daemon`         |

Click the menubar item for the full status, sensor timestamp,
manual refresh, and force-reauthenticate actions.

## Configuration

All settings come from `.env` (or environment variables). The full set:

| Var                       | Default              | Notes                                        |
|---------------------------|----------------------|----------------------------------------------|
| `LIBRELINKUP_EMAIL`       | (required)           |                                              |
| `LIBRELINKUP_PASSWORD`    | (required)           |                                              |
| `LIBRELINKUP_REGION`      | `US`                 | cluster name or country code                 |
| `POLL_INTERVAL_SECONDS`   | `60`                 | how often the daemon hits Abbott             |
| `STALE_AFTER_SECONDS`     | `360`                | sensor timestamp older than this → `stale`   |
| `DAEMON_HOST`             | `127.0.0.1`          | bind address; do not expose publicly         |
| `DAEMON_PORT`             | `8765`               |                                              |
| `DAEMON_STATE_DIR`        | `~/.glucose-display` | lockfile, session cache, rotating log        |
| `LOG_LEVEL`               | `INFO`               | `DEBUG` for verbose                          |
| `ERROR_BACKOFF_BASE_SECONDS`   | `10`            | first wait after a network failure           |
| `ERROR_BACKOFF_CAP_SECONDS`    | `300`           | max wait between retries                     |
| `RATELIMIT_BACKOFF_BASE_SECONDS` | `30`          | first wait after a 429 with no `Retry-After` |
| `RATELIMIT_BACKOFF_CAP_SECONDS`  | `600`         | max rate-limit wait                          |
| `DAEMON_URL` (frontend)   | `http://127.0.0.1:8765` |                                          |
| `FRONTEND_POLL_SECONDS`   | `30`                 | how often the menubar refreshes from daemon  |
| `THRESHOLD_LOW_MG_DL`     | `70`                 | menubar text turns red below this            |
| `THRESHOLD_HIGH_MG_DL`    | `180`                | turns yellow above this                      |
| `THRESHOLD_VERY_HIGH_MG_DL` | `250`              | turns orange above this                      |

### Menubar colour scheme

The numeric value in the menubar is colour-coded by `value_in_mg_per_dl`
(so mmol/L users still get correct colours):

| Range (mg/dL)         | Colour | Meaning      |
|-----------------------|--------|--------------|
| `< 70`                | red    | hypo         |
| `70 – 180`            | green  | in range     |
| `> 180 – 250`         | yellow | high         |
| `> 250`               | orange | very high    |

Boundary values (exactly 70, 180, 250) sit on the *lower-severity* side —
e.g. 70 is green, 250 is yellow — so you don't get nagged for being on
the edge of a target. Override via the env vars above. Error glyphs
(`⚠ auth`, `⚠ net`, etc.) use the system default text colour.

## Operational notes (Phase 3)

The daemon persists its LibreView JWT to `~/.glucose-display/session.json`
(mode 0600) and reuses it across restarts, so a reboot or `launchctl kickstart`
no longer triggers a fresh Abbott login. A token that the server rejects mid-poll
triggers a single silent refresh; repeated failures fall back to exponential
backoff with jitter.

Network errors and rate-limit responses use independent backoff counters:
one stretched-out `ConnectionError` run won't cause the next 429 to start
already-escalated. A successful poll resets both.

Sensor warm-up (the first 60 minutes of a new sensor) is reported as
`warming_up` — the daemon knows from the sensor metadata that measurements
are deliberately absent, so the menubar shows the warm-up glyph instead of
a network error.

Logs are written to `~/.glucose-display/daemon.log` (1 MB × 3 backups). The
daemon also installs `SIGTERM`/`SIGINT` handlers so `launchctl stop` exits
cleanly, writing a `Poller stopped` line before returning.

Run the test suite:

```bash
.venv/bin/python -m pytest tests/
```

## What it deliberately does not do (yet)

- Run as a LaunchAgent, store creds in Keychain, or bundle as a `.app`
- Port to Linux (AppIndicator / tray)

These are tracked in the [implementation plan](./glucose-menubar-implementation-plan.md).
