# Glucobar

A macOS menubar glucose display backed by LibreView.

- `daemon` polls LibreView and serves local HTTP on `127.0.0.1:8765`.
- `macos_frontend` is a menubar app (`rumps`) that reads from the daemon.
- Credentials live in the macOS Keychain only — there is no `.env`
  fallback for the email/password, and the menubar prompts for them on
  first launch.

For how the current code fits together, see
[`ENGINEERING.md`](./ENGINEERING.md). For the design rationale
(why this shape rather than another), see
[`glucose-menubar-implementation-plan.md`](./glucose-menubar-implementation-plan.md).
For release and Homebrew publishing steps, see
[`RELEASING.md`](./RELEASING.md).
For the forward-looking Linux port, see
[`kde-fedora-implementation-plan.md`](./kde-fedora-implementation-plan.md).

## ⚠️ Disclaimer

**Glucobar is not a medical device.** It is not intended for diagnosis,
treatment, or any clinical decision-making. Do not rely on it for
therapy adjustments — check your sensor or meter directly.

Glucobar is an independent open-source project. It is **not affiliated
with, endorsed by, or sponsored by Abbott Laboratories**. "FreeStyle
Libre", "LibreLink", "LibreLinkUp", and "LibreView" are trademarks of
Abbott. Glucobar uses your own LibreView account credentials to read
data from Abbott's servers on your behalf; no data is redistributed.

Use at your own risk. See [`LICENSE`](./LICENSE) (Apache-2.0) for the
full warranty disclaimer.

## Requirements

- macOS 11+ (frontend uses AppKit via PyObjC) with internet access.
- Python 3.11+.
- A **FreeStyle Libre** sensor (Libre 2 / 2 Plus / 3 / 3 Plus) that
  you're actively wearing.
- The **FreeStyle LibreLink** app installed on your phone, paired to
  the sensor, and running in the background with internet access.
  Glucobar does **not** talk to the sensor directly — it reads the
  uploads your phone makes to LibreView, so your phone has to keep
  streaming them. In practice that means:
  - LibreLink running (it keeps a background Bluetooth/HTTPS session
    going; iOS and Android will both eventually suspend it if you
    force-quit the app).
  - The phone online over Wi-Fi or cellular.
  - The phone within Bluetooth range of the sensor for Libre 2/3
    (continuous streaming); Libre 1 is scan-only and won't work.
- A **LibreView account** with at least one shared sensor via
  **LibreLinkUp**. Glucobar signs in with this account's email and
  password — the same ones you use on libreview.com. You do **not**
  need a separate LibreLinkUp follower account; Glucobar reads the
  connections attached to the main LibreView account it signs in as.
- The phone's LibreView region must match Glucobar's region setting.
  By default, Glucobar infers it from your macOS locale (US fallback);
  you can override with `LIBREVIEW_REGION` in `.env` if needed (see
  [Configuration](#configuration)).

If your phone is offline, asleep with LibreLink force-quit, or out of
Bluetooth range for too long, the menubar will fall back to
`Connecting…` or show a stale reading (`124 ↗*`) once the last value
ages past `STALE_AFTER_SECONDS`. Bringing the phone back online
resumes the stream — no action needed in Glucobar.

## Quick Start (from source)

Glucobar is currently distributed as source. A local standalone `.app`
build is now available via `py2app` (see
[Build a Standalone App (py2app)](#build-a-standalone-app-py2app)).
GitHub-hosted release artifacts are available via the release workflow
(see [Packaging roadmap](#packaging-roadmap)).

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
scripts/install_launchagent.sh install
```

Optional (`.env` is not required for first run):

```bash
cp .env.example .env
open -e .env
```

If you use `zsh` and paste command blocks with `# ...` comment lines, run
`setopt interactive_comments` first (or paste only the raw commands).

On first launch the menubar prompts for your LibreView email and
password and saves them to the Keychain. If you prefer the terminal,
you can seed them up front instead:

```bash
.venv/bin/python scripts/set_credentials.py
```

To verify your account + region end-to-end against LibreView:

```bash
.venv/bin/python scripts/phase0_probe.py
```

**Managed services** means one LaunchAgent job: `launchd` starts the
menubar at login, and the menubar supervises the daemon as a child
process (respawns it on crash, tears it down on Quit).

**Open at Login** is a real `launchctl` toggle for that
frontend LaunchAgent (`com.glucobar.frontend`):

- checked = startup at next login is enabled
- unchecked = startup at next login is disabled

Quitting from the menu still exits the app for the current session;
it does not immediately relaunch. Check status/logs:

```bash
scripts/install_launchagent.sh status
scripts/install_launchagent.sh logs
```

## Run Without Auto-start

Use the app bundle directly without enabling login auto-start. The
menubar still supervises the daemon automatically:

```bash
scripts/install_launchagent.sh install
scripts/install_launchagent.sh disable
open build/Glucobar.app
```

Quitting the menubar (⌘Q or **Quit**) SIGTERMs the supervised daemon.

**Running the daemon separately** (e.g. to iterate on daemon code in
one terminal and see frontend changes in another): start the daemon
first, then start the menubar — the menubar probes
`${DAEMON_URL}/health` on boot, sees a reply, and runs in passive
"external" mode without spawning its own child.

```bash
.venv/bin/python -m daemon            # terminal 1
open build/Glucobar.app               # terminal 2 — detects terminal 1
```

The supervisor is always wired up in the frontend — there is no
opt-out env var. When a daemon is already listening it stays passive;
when nothing is listening it spawns and owns the child. That keeps the
deployment topology strictly "one wrapper, one LaunchAgent, one
supervisor" regardless of how you launch the menubar.

## Build a Standalone App (py2app)

This builds a portable app bundle at `dist/Glucobar.app` that includes
its own Python runtime and dependencies (no repo-relative `.venv`
symlinks).

```bash
# From repo root
python3 -m venv .venv
.venv/bin/python -m pip install -e .[dev,build]
scripts/build_portable_app.sh
open dist/Glucobar.app
```

Notes:

- This is the first packaging step toward release artifacts; the app is
  not signed or notarized yet.
- `scripts/install_launchagent.sh` still manages the source-wrapper flow
  (`build/Glucobar.app`). If you want launch-at-login for a `py2app`
  build, ship/update a release-specific LaunchAgent plist that points to
  `dist/Glucobar.app/Contents/MacOS/Glucobar`.

## Package a Release Zip (Homebrew-ready)

After `dist/Glucobar.app` exists, package a release zip + SHA256:

```bash
# Uses project version from pyproject.toml (e.g. v0.3.0)
scripts/package_release_artifact.sh --repo your-org/glucobar
```

Outputs:

- `release/Glucobar-vX.Y.Z-macos.zip`
- `release/Glucobar-vX.Y.Z-macos.zip.sha256`
- `release/homebrew-cask-snippet.rb` (ready to paste into a tap cask)

If needed, override the version tag explicitly:

```bash
scripts/package_release_artifact.sh --version v0.3.0 --repo your-org/glucobar
```

## Daily Commands

`scripts/install_launchagent.sh install` re-applies the frontend LaunchAgent plist, rebuilds `build/Glucobar.app` when needed, and restarts the menubar (which respawns its supervised daemon child) — use it after code or `.env` changes, or after recreating the venv. It also sweeps any legacy LaunchAgents (`com.glucobar.daemon`, `com.glucose-display.*`) left behind by older installs so two daemons can never race the same LibreView session.

`scripts/install_launchagent.sh uninstall` unregisters the LaunchAgent so `launchd` stops auto-starting the menubar and removes the plist file from `~/Library/LaunchAgents/`. Launch the app manually with `open build/Glucobar.app` if you still want to run it in the current session.

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
to the row), **Open at Login** (enables/disables next-login
autostart for `com.glucobar.frontend`), **LibreView Account**, **Quit**.

## Configuration

All non-credential settings come from environment variables (usually
`.env`). Credentials are stored in the macOS Keychain only and never
read from env vars or `.env`.

### Required

- LibreView email + password in the Keychain under service name
  `glucobar` — the menubar prompts for these on first launch, or you
  can seed them via `scripts/set_credentials.py`.

### Common Optional

- `LIBREVIEW_REGION` (cluster like `US`, `EU`, `EU2`, `DE`, `FR`, `JP`, `AU`, `CA`, `AE`, `AP`, `LA`, `RU`; default inferred from locale, fallback `US`)
- `LIBREVIEW_REGION` also accepts country codes that map to clusters (for example `PT`/`ES`/`GB` -> `EU`)
- `POLL_INTERVAL_SECONDS` (default `60`)
- `FRONTEND_POLL_SECONDS` (default `30`)
- `STALE_AFTER_SECONDS` (default `360`)
- `DAEMON_URL` (default `http://127.0.0.1:8765`)
- `THRESHOLD_LOW_MG_DL` (default `70`)
- `THRESHOLD_HIGH_MG_DL` (default `180`)
- `THRESHOLD_VERY_HIGH_MG_DL` (default `250`)
- `DAEMON_STATE_DIR` (default `~/.glucobar`)
- `LOG_LEVEL` (default `INFO`)

`.env.example` covers the most common knobs; this section is the canonical
list of currently supported environment variables.

## Credentials and Dialog Behavior

- **Source of truth**: macOS Keychain only. There is no env var /
  `.env` fallback for email or password — if the Keychain is empty
  the menubar prompts on next launch.
- **The credentials dialog**: a single window with stacked email +
  password fields shown in a native `NSPanel`. Same code path for
  both first-launch onboarding and the **LibreView Account** menu
  item — onboarding just swaps the window copy.
- **After you save**: the menubar writes the Keychain entries and
  asks the supervised daemon to restart so it re-authenticates
  immediately with the new credentials.
- **Terminal helpers** (alternative to the in-app prompt):
  - `scripts/set_credentials.py` — prompt + save
  - `scripts/set_credentials.py --show` — prints the stored email
  - `scripts/set_credentials.py --clear` — removes the Keychain entries

## Privacy

Glucobar talks directly to LibreView's servers using the credentials
you provide, and to `127.0.0.1:8765` between the daemon and the
menubar. There is no telemetry, no analytics, and no third-party
server in the loop.

State lives under `~/.glucobar/` (configurable via `DAEMON_STATE_DIR`):

- `daemon.lock` — single-instance guard
- `session.json` — cached LibreView session token
- `daemon.log` (+ `.1`..`.3`) — rotating log (1 MB × 3 backups)

Credentials live in the macOS Keychain under service name `glucobar`.
Remove them with `scripts/set_credentials.py --clear` or via
**Keychain Access.app**.

## Upgrading from v0.3.x

If you had `LIBRELINKUP_EMAIL`, `LIBRELINKUP_PASSWORD`, or
`LIBRELINKUP_REGION` in your `.env`:

1. Credentials are Keychain-only now. If your Keychain already has the
   `glucobar` service entry from a previous run of
   `scripts/set_credentials.py`, you're set. Otherwise let the menubar
   prompt you on next launch, or run the script once.
2. Rename `LIBRELINKUP_REGION` to `LIBREVIEW_REGION` in `.env`. The
   old name is no longer read.
3. Delete `LIBRELINKUP_EMAIL` / `LIBRELINKUP_PASSWORD` from `.env` —
   they're ignored and only clutter the file.

## Upgrading from v0.2.x (`glucose-display`)

Glucobar used to be called `glucose-display`. If you're upgrading:

1. `scripts/install_launchagent.sh install` will automatically bootout
   the old `com.glucose-display.daemon` / `com.glucose-display.frontend`
   LaunchAgents and remove `build/Glucose Menubar.app`.
2. The Keychain service name changed from `librelinkup-glucose-menubar`
   to `glucobar`. Let the menubar re-prompt on next launch, or re-run
   `scripts/set_credentials.py` once; then delete the old entry in
   **Keychain Access.app**.
3. The daemon state directory moved from `~/.glucose-display` to
   `~/.glucobar`. The new one is created on first run; you can delete
   the old directory or `mv` it over if you want to keep logs.

## Troubleshooting

- **Account/region check fails**: run `.venv/bin/python scripts/phase0_probe.py` and fix the region / Keychain entry.
- **"Another glucose daemon is already running"**: the supervisor detected another daemon holding `~/.glucobar/daemon.lock`. Quit the other one (e.g. a stray `python -m daemon` in another terminal), then relaunch the menubar.
- **Menubar keeps restarting**: `scripts/install_launchagent.sh logs` and inspect frontend errors.
- **Daemon exits immediately with "No LibreView credentials found"**: open the menubar app once so the first-run prompt can seed the Keychain, or run `scripts/set_credentials.py`.
- **Frontend exits immediately when launched from terminal**: expected — direct `python -m macos_frontend` runs are unsupported. Launch via `open build/Glucobar.app` or `scripts/install_launchagent.sh install`.
- **Rebuilt `.venv` or upgraded Python**: rerun `scripts/install_launchagent.sh install` to rebuild the wrapper app symlinks.

## Development

```bash
.venv/bin/python -m pytest tests/
.venv/bin/python -m ruff check .
```

## Packaging roadmap

The current `build/Glucobar.app` is a thin shim around the developer's
`.venv` — it's only portable on the machine that built it. Work in
progress toward a downloadable build:

- [x] Single-process entry point (menubar supervises the daemon as a child)
- [x] `py2app` config producing a standalone `Glucobar.app`
- [ ] App icon (`resources/Glucobar.icns`)
- [x] Onboarding / preferences window for first-launch credential entry
- [x] GitHub Actions release workflow (build + test + zip + upload release assets)
- [ ] Homebrew Cask tap
- [ ] (Stretch) Developer ID signing + notarization

Install from source using the instructions above, or use the release workflow
artifacts when you want a standalone zip build.

## Contributing

Issues and pull requests welcome, with the understanding that Glucobar
is a personal-scope menubar utility — feature scope will stay small.
Please run `pytest` and `ruff check` before opening a PR. Bug reports
that include `scripts/phase0_probe.py` output and your LibreView
region will be triaged fastest.

## License

Apache-2.0. See [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE).
