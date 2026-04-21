# librelinkup-glucose-menubar-python

A personal macOS menubar app that shows your current FreeStyle Libre
glucose value via LibreLinkUp. A small FastAPI daemon talks to Abbott;
a thin `rumps` menubar app polls the daemon over HTTP. See
[`glucose-menubar-implementation-plan.md`](./glucose-menubar-implementation-plan.md)
for design details.

## Quick start

```bash
# 1. Virtualenv + deps (Python 3.11+)
python3 -m venv .venv
.venv/bin/pip install pylibrelinkup rumps python-dotenv keyring \
                     fastapi uvicorn pydantic-settings httpx

# 2. Credentials — pick one
.venv/bin/python scripts/set_credentials.py     # macOS Keychain (recommended)
cp .env.example .env && $EDITOR .env            # or .env file

# 3. Run daemon + menubar in one terminal (Ctrl+C stops both)
(.venv/bin/python -m daemon &) ; DAEMON_PID=$! ; .venv/bin/python -m macos_frontend ; kill $DAEMON_PID 2>/dev/null
```

Keychain wins over `.env`. `scripts/set_credentials.py --show` reports
the active source; `--clear` removes the Keychain entries.

`LIBRELINKUP_REGION` accepts cluster names (`US`, `EU`, `EU2`, `DE`,
`FR`, `JP`, `AU`, `CA`, `AE`, `AP`, `LA`, `RU`) or country codes that
map to one (e.g. `PT`, `ES`, `GB` → `EU`).

## Running

The daemon owns all LibreLinkUp interaction and serves
`http://127.0.0.1:8765`. The menubar app is a thin HTTP client.

**Both, one terminal (Ctrl+C stops both):**

```bash
(.venv/bin/python -m daemon &) ; DAEMON_PID=$! ; .venv/bin/python -m macos_frontend ; kill $DAEMON_PID 2>/dev/null
```

**Menubar only** (daemon already running from LaunchAgent / another terminal):

```bash
.venv/bin/python -m macos_frontend
```

**Daemon only:**

```bash
.venv/bin/python -m daemon
```

**Everything in one file** (no daemon split — hits Abbott directly):

```bash
.venv/bin/python mvp.py
```

Probe the daemon by hand, or open <http://127.0.0.1:8765/docs>:

```bash
curl -s http://127.0.0.1:8765/glucose/current | jq
curl -s -X POST http://127.0.0.1:8765/auth/refresh | jq
```

## Menubar status

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

Click the icon for status details, **Refresh now**, **Force
re-authenticate**, **Update credentials…**, **Set thresholds…**,
**Notifications** toggle, and **Quit**.

### Colour scheme

The numeric value is colour-coded by `value_in_mg_per_dl` (mmol/L users
still get correct colours):

| Range (mg/dL) | Colour | Meaning   |
|---------------|--------|-----------|
| `< 70`        | red    | hypo      |
| `70 – 180`    | green  | in range  |
| `> 180 – 250` | yellow | high      |
| `> 250`       | orange | very high |

Boundary values (70, 180, 250) sit on the *lower-severity* side so you
don't get nagged on the edge of a target. Override via
`THRESHOLD_*_MG_DL` (see below). Error glyphs use the default text colour.

## Configuration

All settings come from environment variables (typically via `.env`).

| Var                              | Default                 | Notes                                         |
|----------------------------------|-------------------------|-----------------------------------------------|
| `LIBRELINKUP_EMAIL`              | (required)              |                                               |
| `LIBRELINKUP_PASSWORD`           | (required)              |                                               |
| `LIBRELINKUP_REGION`             | `US`                    | cluster name or country code                  |
| `POLL_INTERVAL_SECONDS`          | `60`                    | how often the daemon hits Abbott              |
| `STALE_AFTER_SECONDS`            | `360`                   | sensor timestamp older than this → `stale`    |
| `DAEMON_HOST`                    | `127.0.0.1`             | bind address; do not expose publicly          |
| `DAEMON_PORT`                    | `8765`                  |                                               |
| `DAEMON_STATE_DIR`               | `~/.glucose-display`    | lockfile, session cache, rotating log         |
| `LOG_LEVEL`                      | `INFO`                  | `DEBUG` for verbose                           |
| `ERROR_BACKOFF_BASE_SECONDS`     | `10`                    | first wait after a network failure            |
| `ERROR_BACKOFF_CAP_SECONDS`      | `300`                   | max wait between retries                      |
| `RATELIMIT_BACKOFF_BASE_SECONDS` | `30`                    | first wait after a 429 with no `Retry-After`  |
| `RATELIMIT_BACKOFF_CAP_SECONDS`  | `600`                   | max rate-limit wait                           |
| `DAEMON_URL` (frontend)          | `http://127.0.0.1:8765` |                                               |
| `FRONTEND_POLL_SECONDS`          | `30`                    | how often the menubar refreshes from daemon   |
| `THRESHOLD_LOW_MG_DL`            | `70`                    | menubar text turns red below this             |
| `THRESHOLD_HIGH_MG_DL`           | `180`                   | turns yellow above this                       |
| `THRESHOLD_VERY_HIGH_MG_DL`      | `250`                   | turns orange above this                       |
| `NOTIFY_ENABLED`                 | `1`                     | set `0` to silence all notifications          |
| `NOTIFY_COOLDOWN_SECONDS`        | `900`                   | gap before the same zone can re-fire          |
| `NOTIFY_STALE_AFTER_SECONDS`     | `600`                   | how old a reading must be to trigger an alert |

## Notifications

The frontend fires a macOS notification on each transition into
Low / High / Very High, plus one when the sensor goes stale. Noise
controls: **hysteresis** (5 mg/dL margin before re-firing the same
zone), **cooldown** (`NOTIFY_COOLDOWN_SECONDS`, default 15 min), and
**immediate lows** (hypo always fires on the first crossing).

Toggle from the menubar or set `NOTIFY_ENABLED=0`.

## Autostart on login

Two LaunchAgents (daemon + frontend) start both on login, auto-restart
on crash, and log to `~/.glucose-display/launchd.*.log`.

```bash
scripts/install_launchagent.sh install     # render templates + bootstrap
scripts/install_launchagent.sh status      # running / not loaded
scripts/install_launchagent.sh logs        # tail launchd logs
scripts/install_launchagent.sh uninstall   # bootout + remove plists
```

Templates under `scripts/launchagents/` use `__VENV_PYTHON__`,
`__REPO_ROOT__`, and `__STATE_DIR__` placeholders substituted at install
time (launchd has no useful `$PATH`).

## Troubleshooting

**API smoke test.** Verify Abbott is reachable for your account:

```bash
.venv/bin/python scripts/phase0_probe.py
```

**Stop everything and clear cached session** (LaunchAgents, processes,
`session.json`, lockfile). Omit the `rm` clause to keep the stored JWT:

```bash
GUI_DOMAIN="gui/$(id -u)"; launchctl bootout "${GUI_DOMAIN}/com.glucose-display.daemon" 2>/dev/null; launchctl bootout "${GUI_DOMAIN}/com.glucose-display.frontend" 2>/dev/null; lsof -t -nP -iTCP:8765 -sTCP:LISTEN | xargs kill 2>/dev/null; pkill -f "python.*-m macos_frontend" 2>/dev/null; rm -f ~/.glucose-display/session.json ~/.glucose-display/daemon.lock; echo "Stopped and cleared state."
```

**`Another glucose daemon is already running`.** Two processes are
fighting for `~/.glucose-display/daemon.lock`. Find and kill the extra:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
kill "$(lsof -t -nP -iTCP:8765 -sTCP:LISTEN)"
launchctl kickstart -k "gui/$(id -u)/com.glucose-display.daemon"
```

**Menubar icon won't quit.** Frontend and daemon are separate
processes; killing port 8765 only stops the daemon. Quit via the menu,
or:

```bash
pkill -f "python.*-m macos_frontend"
# if LaunchAgent keeps restarting it:
launchctl bootout "gui/$(id -u)/com.glucose-display.frontend"
```

**Menu greys out after clicking Update credentials / Set thresholds.**
Dialogs are deferred a beat so the menu can finish tearing down;
if it still hangs, file an issue with your macOS version.

## Operational notes

- **Session persistence.** Daemon saves the LibreView JWT to
  `~/.glucose-display/session.json` (mode 0600) and reuses it across
  restarts. A mid-poll 401 triggers one silent refresh; repeated
  failures fall back to exponential backoff with jitter.
- **Independent backoff** for network errors vs. rate-limits, so a
  stretched-out outage doesn't pre-escalate the next 429.
- **Sensor warm-up** (first 60 min of a new sensor) is reported as
  `warming_up`, not a network error.
- **Logs:** `~/.glucose-display/daemon.log` (1 MB × 3 backups).
  `SIGTERM`/`SIGINT` exit cleanly (`Poller stopped` line in the log).
- **Packaging:** if you bundle the frontend as a `.app`, set
  `LSUIElement=1` in `Info.plist` or you get a stray Dock icon. Not
  needed when running `python -m macos_frontend` directly.

Run the test suite:

```bash
.venv/bin/python -m pytest tests/
```

## Status

Phases 0–4 (verification, MVP, daemon split, robustness, macOS polish) done.
Phase 5 (Linux port) pending.

## Deliberate non-goals

- Bundling / notarising a `.app` — personal use, runs from source.
- Linux port — tracked in the
  [implementation plan](./glucose-menubar-implementation-plan.md).
