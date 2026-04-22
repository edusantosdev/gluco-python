# librelinkup-glucose-menubar-python

Personal macOS menubar glucose display for LibreLinkUp.

- `daemon` polls LibreLinkUp and serves local HTTP on `127.0.0.1:8765`.
- `macos_frontend` is a menubar app (`rumps`) that reads from the daemon.
- credentials are loaded from macOS Keychain first, then `.env`.

For design details, see [`glucose-menubar-implementation-plan.md`](./glucose-menubar-implementation-plan.md).

## Requirements

- macOS (frontend uses AppKit via PyObjC)
- Python 3.11+
- LibreLinkUp account credentials

## Quick Start (Recommended)

```bash
# 1) Create venv and install deps
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 2) Create .env and fill basic settings
cp .env.example .env
$EDITOR .env

# 3) Verify account/region works
.venv/bin/python scripts/phase0_probe.py

# 4) Save credentials to Keychain (recommended)
.venv/bin/python scripts/set_credentials.py

# 5) Register LaunchAgents with macOS launchd (daemon + menubar, auto-start on login)
scripts/install_launchagent.sh install
```

**Managed services** here means those two LaunchAgent jobs: `launchd` starts the daemon and the menubar for you, restarts them on crash, and brings them back after reboot/login. Check status/logs:

```bash
scripts/install_launchagent.sh status
scripts/install_launchagent.sh logs
```

## Run Without LaunchAgents

Skip `install_launchagent.sh` and start processes yourself—nothing is registered with `launchd`, so nothing auto-starts at login. Run both in one shell:

```bash
.venv/bin/python -m daemon & DAEMON_PID=$!
trap 'kill $DAEMON_PID 2>/dev/null' EXIT
.venv/bin/python -m macos_frontend
```

Run only one side:

```bash
.venv/bin/python -m daemon
.venv/bin/python -m macos_frontend
```

## Daily Commands

```bash
# Re-apply LaunchAgent config and restart both jobs (use after code or .env changes,
# or to rebuild build/Glucose Menubar.app after recreating the venv)
scripts/install_launchagent.sh install

# Unregister both LaunchAgents: stops launchd from auto-starting or restarting them,
# and removes the plist files from ~/Library/LaunchAgents/. Run daemon/menubar manually
# if you still need them, or reinstall later with install.
scripts/install_launchagent.sh uninstall

# Probe daemon API
curl -s http://127.0.0.1:8765/glucose/current | jq
curl -s -X POST http://127.0.0.1:8765/auth/refresh | jq
```

## Menubar Status Titles

| Status | Title |
|---|---|
| `ok` | `124 →` |
| `stale` | `124 →*` |
| `starting` | `Starting…` |
| `warming_up` | `Warming Up` |
| `sensor_ended` | `⚠ Sensor Ended` |
| `no_network` | `⚠ Offline` |
| `auth_failed` | `⚠ Sign In` |
| `tos_required` | `⚠ Accept Terms` |
| `rate_limited` | `⚠ Rate Limited` |
| daemon offline | `⚠ Daemon Offline` |
| daemon error | `⚠ Daemon Error` |

Menu actions: **Refresh Now**, **Force Re-Authenticate**, **Notifications**, **Update Credentials…**, **Set Thresholds…**, **Quit**.

## Configuration

All settings come from environment variables (usually `.env`).

### Required

- `LIBRELINKUP_EMAIL`
- `LIBRELINKUP_PASSWORD`
- `LIBRELINKUP_REGION` (cluster like `US`, `EU`, `EU2`, `DE`, `FR`, `JP`, `AU`, `CA`, `AE`, `AP`, `LA`, `RU`)

`LIBRELINKUP_REGION` also accepts country codes that map to clusters (for example `PT`/`ES`/`GB` -> `EU`).

### Common Optional

- `POLL_INTERVAL_SECONDS` (default `60`)
- `FRONTEND_POLL_SECONDS` (default `30`)
- `STALE_AFTER_SECONDS` (default `360`)
- `DAEMON_URL` (default `http://127.0.0.1:8765`)
- `THRESHOLD_LOW_MG_DL` (default `70`)
- `THRESHOLD_HIGH_MG_DL` (default `180`)
- `THRESHOLD_VERY_HIGH_MG_DL` (default `250`)
- `NOTIFY_ENABLED` (`1`/`0`, default `1`)
- `NOTIFY_COOLDOWN_SECONDS` (default `900`)
- `NOTIFY_STALE_AFTER_SECONDS` (default `600`)
- `DAEMON_STATE_DIR` (default `~/.glucose-display`)
- `LOG_LEVEL` (default `INFO`)

See `.env.example` for the full list.

## Credentials and Dialog Behavior

- Keychain is preferred over `.env` automatically.
- `scripts/set_credentials.py --show` prints active source.
- `scripts/set_credentials.py --clear` removes Keychain entries.
- **Update Credentials…** in menubar:
  - bundled launch (`install_launchagent.sh install`): native single-window email+password dialog
  - terminal launch (`python -m macos_frontend`): AppleScript fallback prompts

## Troubleshooting

- **Account/region check fails**: run `.venv/bin/python scripts/phase0_probe.py` and fix `.env` values.
- **`Another glucose daemon is already running`**: stop the other daemon process, then rerun install.
- **Menubar keeps restarting**: `scripts/install_launchagent.sh logs` and inspect frontend errors.
- **Dialogs do nothing in terminal mode**: expected fallback behavior; use LaunchAgent install for native dialogs.
- **Rebuilt `.venv` or upgraded Python**: rerun `scripts/install_launchagent.sh install` to rebuild the wrapper app symlinks.

## Development

```bash
.venv/bin/python -m pytest tests/
```

Non-goals: notarizing/codesigning the `.app`, Linux port.
