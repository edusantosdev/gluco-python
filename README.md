# GlucoBar

GlucoBar is a macOS menubar app that shows your current LibreView glucose value and trend.

- `daemon/` polls LibreView and serves local HTTP on `127.0.0.1:8765`.
- `macos_frontend/` is a menubar UI (`rumps` + AppKit) that reads from the daemon.
- LibreView credentials are stored in the macOS Keychain only (not in `.env`).

For internals and future direction, see `ENGINEERING.md`.  
For releases, see `RELEASING.md`.

## Disclaimer

GlucoBar is not a medical device and is not for diagnosis/treatment decisions.
It is an independent open-source project and is not affiliated with Abbott.

## Requirements

- macOS 11+
- Python 3.11+
- Active Libre sensor + LibreLink phone app uploading to LibreView
- LibreView account credentials

## Daily Use (bundled `.app`)

Build once, then launch directly from Finder without re-running setup:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .[dev,build]
scripts/build_portable_app.sh
open dist/GlucoBar.app
```

After this, you can run `dist/GlucoBar.app` directly (or move it to `/Applications`).

## Quick Start (from source / development)

```bash
python3.11 -m venv .venv
.venv/bin/python -V  # should be 3.11+
.venv/bin/python -m pip install -e .
scripts/install_launchagent.sh install
```

Optional `.env` for non-credential settings:

```bash
cp .env.example .env
open -e .env
```

First launch prompts for LibreView credentials and stores them in Keychain.
You can also set credentials from terminal:

```bash
.venv/bin/python scripts/set_credentials.py
```

Verify account + region quickly:

```bash
.venv/bin/python scripts/phase0_probe.py
```

## Runtime

One LaunchAgent (`com.glucobar.frontend`) starts the menubar at login.
The menubar then supervises the daemon as a child process. If a daemon
is already running (e.g. you started one in a terminal), the menubar
attaches to it instead of spawning its own.

Manage the install:

```bash
scripts/install_launchagent.sh install    # apply/restart current install
scripts/install_launchagent.sh uninstall  # remove login autostart
scripts/install_launchagent.sh status
scripts/install_launchagent.sh logs
```

Note: the app's `Open at Login` menu item only enables/disables an existing
LaunchAgent plist. If no plist is installed yet, run
`scripts/install_launchagent.sh install` once first.

Poke the daemon API directly:

```bash
curl -s http://127.0.0.1:8765/glucose/current | jq
curl -s -X POST http://127.0.0.1:8765/auth/refresh | jq
```

## Menubar Status

- Fresh reading: `124 ↗`
- Stale with last value: `124 ↗*`
- Starting/warming/rate-limited: `Connecting…`
- No network/daemon unreachable: `⚠ Offline`
- Auth/ToS issue: `⚠ Sign In`
- Unexpected daemon failure: `⚠ Error`

Menu actions:

- `Refresh` — force daemon re-auth + immediate UI update
- `Open at Login` — toggle LaunchAgent enable/disable for next login
- `Sign In to / Sign Out of LibreView Account` — manage Keychain credentials
- `Quit`

## Configuration

Credentials are Keychain-only. Only the menubar app (`GlucoBar`) reads
Keychain; it forwards credentials to the daemon subprocess
(`GlucoBarDaemon`) via env, so you only see a single "GlucoBar wants to
access the glucobar Keychain item" dialog — click **Always Allow** and
you're set. The bundled `.app` is ad-hoc code-signed by
`scripts/build_portable_app.sh` so the grant persists across launches
of the same build.

Common `.env` variables:

- `LIBREVIEW_REGION` (cluster, default inferred from locale)
- `DAEMON_URL` (default `http://127.0.0.1:8765`)
- `DAEMON_STATE_DIR` (default `~/.glucobar`)
- `LOG_LEVEL` (default `INFO`)
- `ERROR_BACKOFF_BASE_SECONDS` / `ERROR_BACKOFF_CAP_SECONDS`
- `RATELIMIT_BACKOFF_BASE_SECONDS` / `RATELIMIT_BACKOFF_CAP_SECONDS`

Fixed runtime defaults:

- daemon poll interval: 60s
- frontend poll interval: 30s
- stale cutoff: 360s
- color thresholds: `<70`, `70-180`, `180-250`, `>250` mg/dL

## Standalone App Build (py2app)

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .[dev,build]
scripts/build_portable_app.sh
open dist/GlucoBar.app
```

Create release zip + SHA:

```bash
scripts/package_release_artifact.sh --repo <owner>/<repo>
```

For the full release flow (tag, publish, update tap), see `RELEASING.md`.

## Troubleshooting

- Account/region issues: run `.venv/bin/python scripts/phase0_probe.py`.
- Missing credentials: open the app once or run `.venv/bin/python scripts/set_credentials.py`.
- Rebuilt venv / upgraded Python: rerun `scripts/install_launchagent.sh install`.
- `Package 'gluco-bar' requires a different Python` (for example 3.9.x): recreate
  `.venv` with Python 3.11+.
- Editable install fails because of old packaging tooling: run
  `.venv/bin/python -m pip install --upgrade pip setuptools wheel`.
- "Another glucose daemon is already running": stop any manual `python -m daemon`, then relaunch.
- Check daemon logs: `scripts/install_launchagent.sh logs` or tail `~/.glucobar/daemon.log`.

## Development

```bash
.venv/bin/python -m pytest tests/
.venv/bin/python -m ruff check .
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
