# librelinkup-glucose-menubar-python

A personal macOS menubar app that shows your current FreeStyle Libre
glucose value via LibreLinkUp. A small FastAPI daemon talks to Abbott;
a thin `rumps` menubar app polls the daemon over HTTP. See
[`glucose-menubar-implementation-plan.md`](./glucose-menubar-implementation-plan.md)
for design details.

## Quick start

```bash
# 1. Virtualenv + deps (Python 3.11+; macOS only — frontend uses AppKit)
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 2. Sanity-check the LibreLinkUp account before going further
cp .env.example .env && $EDITOR .env          # fill in email/password/region
.venv/bin/python scripts/phase0_probe.py      # prints a JSON reading on success

# 3. Move credentials into the macOS Keychain (recommended; you can also
#    leave them in .env, or set them later from the menubar)
.venv/bin/python scripts/set_credentials.py

# 4a. Recommended: install both LaunchAgents. This builds the
#     build/Glucose Menubar.app wrapper, renders the plists, and starts
#     the daemon + menubar so they auto-start on every login.
scripts/install_launchagent.sh install

# 4b. Or run daemon + menubar by hand (Ctrl+C in the second terminal stops
#     the menubar; the trap kills the daemon when that shell exits):
.venv/bin/python -m daemon & DAEMON_PID=$!
trap 'kill $DAEMON_PID 2>/dev/null' EXIT
.venv/bin/python -m macos_frontend
```

Keychain wins over `.env`. `scripts/set_credentials.py --show` reports
the active source; `--clear` removes the Keychain entries. You can
also set / change credentials from the menubar at any time via
**Update Credentials…** — when launched via the LaunchAgent, that
opens a single native window with email + password fields; in plain
terminal mode it falls back to two AppleScript prompts.

`LIBRELINKUP_REGION` accepts cluster names (`US`, `EU`, `EU2`, `DE`,
`FR`, `JP`, `AU`, `CA`, `AE`, `AP`, `LA`, `RU`) or country codes that
map to one (e.g. `PT`, `ES`, `GB` → `EU`).

## Running

The daemon owns all LibreLinkUp interaction and serves
`http://127.0.0.1:8765`. The menubar app is a thin HTTP client. Pick
whichever combination matches what you need:

**Background (recommended).** LaunchAgents start both on login and
auto-restart on crash:

```bash
scripts/install_launchagent.sh install
```

**Both, one terminal (Ctrl+C stops the menubar; trap kills the daemon):**

```bash
.venv/bin/python -m daemon & DAEMON_PID=$!
trap 'kill $DAEMON_PID 2>/dev/null' EXIT
.venv/bin/python -m macos_frontend
```

**Menubar only** (daemon already running from LaunchAgent or another
terminal — note: dialogs degrade to AppleScript prompts in this mode):

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
| `sensor_ended` | `⚠ sensor`         |
| `no_network`   | `⚠ net`            |
| `auth_failed`  | `⚠ auth`           |
| `tos_required` | `⚠ ToS`            |
| `rate_limited` | `⚠ rate`           |
| daemon down    | `⚠ daemon`         |

Click the icon for status details, **Refresh Now**, **Force
Re-Authenticate**, **Update Credentials…**, **Set Thresholds…**,
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
scripts/install_launchagent.sh install     # build .app, render plists, bootstrap
scripts/install_launchagent.sh status      # running / not loaded
scripts/install_launchagent.sh logs        # tail launchd logs
scripts/install_launchagent.sh uninstall   # bootout + remove plists
```

The installer builds a thin `build/Glucose Menubar.app` wrapper bundle
(symlinks to the real venv — no py2app, no rebundle-on-every-edit loop)
and points the frontend LaunchAgent at its `Contents/MacOS/Glucose Menubar`
executable. The bundle gives AppKit a real `CFBundleIdentifier` and
`LSUIElement`, which is what lets us show native dialogs (e.g. the
single-window **Update Credentials…** form) and notifications. When
running directly via `python -m macos_frontend` from a terminal, the
app detects the missing bundle and falls back to AppleScript dialogs.

Templates under `scripts/launchagents/` and `scripts/app_bundle/` use
`__VENV_PYTHON__`, `__REPO_ROOT__`, `__STATE_DIR__`, and
`__APP_EXECUTABLE__` placeholders substituted at install time (launchd
has no useful `$PATH`).

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
pkill -f "Glucose Menubar"            # bundled launch (default)
pkill -f "python.*-m macos_frontend"  # plain terminal launch
# if LaunchAgent keeps restarting it:
launchctl bootout "gui/$(id -u)/com.glucose-display.frontend"
```

**Update Credentials… / Set Thresholds… do nothing.** You're probably
running the frontend from a plain terminal (`python -m macos_frontend`)
without the `.app` bundle. AppKit won't open modal dialogs from an
unbundled process; the app falls back to AppleScript dialogs in that
mode. Either accept the AppleScript prompts (look for them on top of
your other windows — `⌘⇥` to **osascript**), or install via
`scripts/install_launchagent.sh install` to get the native single-window
flow.

**Stale `build/Glucose Menubar.app`.** The bundle just wraps the venv
with symlinks — re-run `scripts/install_launchagent.sh install` after
recreating the venv (the symlink targets change) or after upgrading
Python's minor version (the `Contents/lib/pythonX.Y/site-packages`
symlink is version-specific).

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
- **Packaging:** the `build/Glucose Menubar.app` wrapper produced by
  the installer is a thin symlink shell around the real venv (no
  py2app, no rebundle-on-every-edit). It's small enough to inspect by
  hand: an `Info.plist` for the bundle identity, a `pyvenv.cfg` that
  redirects the bundled Python symlink back to the project's
  `site-packages`, and `Contents/MacOS/Glucose Menubar` symlinking to
  `.venv/bin/python`. See [`scripts/app_bundle/`](./scripts/app_bundle/)
  for the templates.

Run the test suite:

```bash
.venv/bin/python -m pytest tests/
```

## Status

Phases 0–4 (verification, MVP, daemon split, robustness, macOS polish) done.
Phase 5 (Linux port) pending.

## Deliberate non-goals

- Notarising / codesigning the `.app` — personal use, and the bundle
  is a thin wrapper around the repo's venv rather than a shippable
  artifact.
- Linux port — tracked in the
  [implementation plan](./glucose-menubar-implementation-plan.md).
