# GlucoBar

GlucoBar keeps your latest LibreView glucose reading and trend in the macOS
menu bar so you can see whether you are in range, rising, or dropping while
you work. It is not a phone replacement for LibreView; it is a convenience
viewer tied to the same account data.

- `daemon/` polls LibreView and serves local HTTP on `127.0.0.1:8765`.
- `macos_frontend/` is a menubar UI (`rumps` + AppKit) that reads from the daemon.
- LibreView credentials are stored in the macOS Keychain only (not in `.env`).

For internals and future direction, see `ENGINEERING.md`.  
For releases, see `RELEASING.md`.

## Important: your phone must stay connected

GlucoBar does **not** talk to your sensor. It only shows what your phone has
already uploaded to LibreView:

```
Libre sensor ──Bluetooth──▶ Libre app on your phone ──internet──▶ LibreView ──▶ GlucoBar
```

If any link in that chain breaks, GlucoBar stops getting new readings. For
GlucoBar to stay current, **all of the following must be true, all the time**:

- The official Libre app (FreeStyle LibreLink / FreeStyle Libre 3) is
  **running on your phone** — open or in the background. Do not force-quit it
  (on iPhone, don't swipe it away in the app switcher).
- **Bluetooth is on** and the phone is **within Bluetooth range of the
  sensor** (keep it with you, not in another room).
- The phone has a working **internet connection** (Wi-Fi or mobile data) so
  the app can upload to LibreView.
- The phone is not blocking the app in the background — for example, Low
  Power Mode / battery saver, background app refresh turned off, or
  aggressive battery optimisation on Android can delay or stop uploads.
- The sensor is active (not warming up, expired, or failed) and you have
  accepted any pending Terms/Privacy prompts in the Libre app.

When readings stop arriving, GlucoBar keeps showing the last value with an
asterisk (for example `124 ↗*`) once it is more than 6 minutes old. **Treat a
starred value as out of date** — check your phone or meter instead.

## Disclaimer

GlucoBar is **not a medical device** and must not be used for diagnosis,
treatment, dosing, emergency decisions, or any other medical decision.
Glucose values may be delayed, missing, or wrong. Always confirm with your
official LibreView/LibreLink app, a meter, or a qualified professional.

This is an **independent open-source** project. It is not affiliated with,  
endorsed by, or supported by Abbott. Abbott trademarks (for example  
FreeStyle Libre, LibreLink, LibreView) are property of Abbott Laboratories.

## License, notices, and third-party credit

- **License:** [Apache-2.0](LICENSE) — see `LICENSE` for the full text.
- **NOTICE:** Project and third-party attribution summary (`NOTICE`).
- **Open source and assets:** [ATTRIBUTIONS.md](ATTRIBUTIONS.md) — runtime
dependencies, build tools, the droplet icon source, and how to verify
transitive licenses.
- **Copyright:** A copyright line in `NOTICE` and in the app bundle
(`NSHumanReadableCopyright`) is the usual way to record authorship under
Apache-2.0. It is not a substitute for professional legal advice about
liability (see the disclaimer above).



## Requirements

- macOS 11+
- Python 3.11+
- Active Libre sensor + LibreLink phone app uploading to LibreView — the
  phone app must stay running, with Bluetooth and internet on (see
  [Important: your phone must stay connected](#important-your-phone-must-stay-connected))
- LibreView account credentials



## Install from a GitHub release

1. Download `GlucoBar-vX.Y.Z-macos.zip` from the
   [Releases](https://github.com/edusantosdev/glucobar-python/releases) page.
   Optionally check it against the `.sha256` file:
   `shasum -a 256 -c GlucoBar-vX.Y.Z-macos.zip.sha256`
2. Unzip it and move `GlucoBar.app` to `/Applications`.
3. Open it (see below for the first-launch warning).

### First launch: "Apple could not verify…" warning

Release builds are **not signed with an Apple Developer ID and not notarized
by Apple**. Notarization is Apple's automated malware scan for apps
distributed outside the App Store, and it requires a paid Apple Developer
account. It is not a code review. Without it, Gatekeeper blocks the first
launch with a message like *"GlucoBar" Not Opened* or *Apple could not verify
"GlucoBar" is free of malware*. This does not mean the app was found to be
harmful; macOS just has no Apple-issued signature to check.

To open it anyway (needed only once per downloaded version):

- **macOS 15 Sequoia and later:** try to open the app, dismiss the warning,
  then go to **System Settings → Privacy & Security**, scroll down to the
  message about GlucoBar, click **Open Anyway**, and confirm.
- **macOS 14 and earlier:** right-click (or Control-click) `GlucoBar.app` in
  Finder, choose **Open**, then click **Open** in the dialog.
- **Terminal alternative (any version):** remove the download quarantine flag:

  ```bash
  xattr -dr com.apple.quarantine /Applications/GlucoBar.app
  ```

Only do this for a zip you downloaded from this repository's Releases page.
If you'd rather not trust a prebuilt binary, build the app from source
(below). It's the same code, built on your own machine.

## Build and run (`.app`)

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,build]'
scripts/build_portable_app.sh
open dist/GlucoBar.app
```

After building, you can launch `dist/GlucoBar.app` directly from Finder
(or move it to `/Applications`).

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

The menubar supervises the daemon as a child process. If a daemon is
already running (e.g. you started one in a terminal), the menubar
attaches to it instead of spawning its own.

## Menubar Status

- Fresh reading: `124 ↗`
- Stale with last value: `124 ↗*`
- Starting/warming/rate-limited: `Connecting…`
- No network/daemon unreachable: `⚠ Offline`
- Auth/ToS issue: `⚠ Sign In`
- Unexpected daemon failure: `⚠ Error`

Menu actions:

- `Refresh` — force daemon re-auth + immediate UI update
- `Open at Login` — toggle macOS login item registration for next login
- `Sign In to / Sign Out of LibreView Account` — manage Keychain credentials
- `About GlucoBar` — version, author, repository link, license, and disclaimer
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



## Packaging for releases

Create release zip + SHA:

```bash
scripts/package_release_artifact.sh --repo <owner>/<repo>
```

For the full release flow (tag, publish, update tap), see `RELEASING.md`.

## Troubleshooting

- Value has a `*` / readings stopped updating: the problem is almost always on
the phone side. Open the Libre app on your phone and check that it shows a
current reading, Bluetooth is on, the phone is near the sensor and online, and
the app wasn't force-quit. Once the phone uploads again, GlucoBar catches up
on its next poll.
- Account/region issues: run `.venv/bin/python scripts/phase0_probe.py`.
- Missing credentials: open the app once or run `.venv/bin/python scripts/set_credentials.py`.
- Rebuilt venv / upgraded Python: rerun `scripts/build_portable_app.sh`.
- `Package 'gluco-bar' requires a different Python` (for example 3.9.x): recreate
`.venv` with Python 3.11+.
- Editable install fails because of old packaging tooling: run
`.venv/bin/python -m pip install --upgrade pip setuptools wheel`.
- zsh reports `no matches found` for extras install: run
`.venv/bin/python -m pip install -e '.[dev,build]'` (quoted). If pasted
comment lines still trigger globbing, avoid comments in pasted blocks or enable
`setopt interactivecomments` in your shell.
- "Another glucose daemon is already running": stop any manual `python -m daemon`, then relaunch.
- Check daemon logs: tail `~/.glucobar/daemon.log`.



## Development

```bash
.venv/bin/python -m pytest tests/
.venv/bin/python -m ruff check .
```

