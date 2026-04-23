# librelinkup-glucose-menubar-python

Personal macOS menubar glucose display for LibreLinkUp.

- `daemon` polls LibreLinkUp and serves local HTTP on `127.0.0.1:8765`.
- `macos_frontend` is a menubar app (`rumps`) that reads from the daemon.
- credentials are loaded from macOS Keychain first, then `.env`.

For design details, see [`glucose-menubar-implementation-plan.md`](./glucose-menubar-implementation-plan.md) (the phased design journal) and [`ENGINEERING.md`](./ENGINEERING.md) (how the current code fits together).

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

`scripts/install_launchagent.sh install` re-applies the LaunchAgent plists, rebuilds `build/Glucose Menubar.app` when needed, and restarts both jobs—use it after code or `.env` changes, or after recreating the venv.

`scripts/install_launchagent.sh uninstall` unregisters both LaunchAgents so `launchd` stops auto-starting and restarting them, removes the plist files from `~/Library/LaunchAgents/`, and leaves you to start the daemon and menubar yourself if you still need them or to run `install` again when you want `launchd` to manage them.

```bash
scripts/install_launchagent.sh install
scripts/install_launchagent.sh uninstall

curl -s http://127.0.0.1:8765/glucose/current | jq
curl -s -X POST http://127.0.0.1:8765/auth/refresh | jq
```

## Menubar Status Titles


The menubar title is the whole UI — the dropdown only carries the graph
and action buttons. Statuses collapse to these titles:

| Condition                                              | Title          |
| ------------------------------------------------------ | -------------- |
| `ok` (fresh reading)                                   | `124 ↗`        |
| `stale` with a last-known value                        | `124 ↗*`       |
| `starting` / `warming_up` / `rate_limited` / stale w/o value | `Connecting…`  |
| `no_network` / daemon unreachable                      | `⚠ Offline`    |
| `auth_failed` / `tos_required`                         | `⚠ Sign In`    |
| daemon 5xx / other unexpected failure                  | `⚠ Error`      |

When the trend is directional (`↓ ↘ ↗ ↑`) the value + arrow gently pulse
in place so a moving reading is visible at a glance; a sideways `→` and
the trailing `*` stale flag are left static.

Menu actions: **Refresh** (daemon `POST /auth/refresh` plus immediate
UI poll — with a dim `Updated now` / `3 min ago` freshness hint appended
to the row), **LibreLinkUp Account**, **Quit**.

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
- `DAEMON_STATE_DIR` (default `~/.glucose-display`)
- `LOG_LEVEL` (default `INFO`)

See `.env.example` for the full list.

## Credentials and Dialog Behavior

- Keychain is preferred over `.env` automatically.
- `scripts/set_credentials.py --show` prints active source.
- `scripts/set_credentials.py --clear` removes Keychain entries.
- **LibreLinkUp Account** in the menubar:
  - bundled launch (`install_launchagent.sh install`): native single-window email+password dialog
  - terminal launch (`python -m macos_frontend`): AppleScript fallback prompts

## Troubleshooting

- **Account/region check fails**: run `.venv/bin/python scripts/phase0_probe.py` and fix `.env` values.
- **"Another glucose daemon is already running"**: stop the other daemon process, then rerun install.
- **Menubar keeps restarting**: `scripts/install_launchagent.sh logs` and inspect frontend errors.
- **Dialogs do nothing in terminal mode**: expected fallback behavior; use LaunchAgent install for native dialogs.
- **Rebuilt `.venv` or upgraded Python**: rerun `scripts/install_launchagent.sh install` to rebuild the wrapper app symlinks.

## Development

```bash
.venv/bin/python -m pytest tests/
```

Non-goals: notarizing/codesigning the `.app`, Linux port.