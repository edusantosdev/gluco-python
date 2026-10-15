# Engineering Overview

Short technical map of the current Gluco architecture, plus a brief
note on where the project may go next.

## Runtime Topology

Gluco is two processes communicating over local HTTP on
`127.0.0.1:8765`:

- Frontend (`macos_frontend/app.py`) — AppKit/`rumps` menubar UI.
- Daemon (`python -m daemon`) — LibreView auth, polling, caching, status.

Process ownership:

- The frontend supervises the daemon as a child process in
  **managed** mode, respawning on crash (with a crash-rate limit) and
  tearing it down on Quit.
- If a daemon is already reachable at `DAEMON_URL` on startup, the
  supervisor switches to **external** mode and leaves lifecycle to
  whoever already owns it (typically a dev running
  `python -m daemon` manually).
- A parent-death pipe (`daemon/parent_watch.py`, wired up via
  `GLUCOBAR_PARENT_PIPE_FD`) ensures the daemon exits even if the
  frontend dies without running signal handlers (e.g. SIGKILL,
  force-quit). No ghost daemons.
- `Open at Login` is implemented as a macOS login item toggle for the
  running app bundle path (not via LaunchAgent enable/disable).

## Daemon (`daemon/`)

Owns all LibreView-facing behavior:

- authenticates with Keychain-provided credentials
- polls readings every 60s and caches current + ~12h of history
- classifies status: `starting`, `ok`, `stale`, `warming_up`,
  `no_network`, `auth_failed`, `tos_required`, `rate_limited`
- applies exponential backoff, separately for network errors and
  rate-limit responses
- exposes a stable local HTTP API
- enforces single-instance via `fcntl.flock` on
  `~/.glucobar/daemon.lock`
- persists a session token (`session.json`, mode 0600) so short
  restarts don't force a fresh login

Missing credentials cause the daemon to exit with code `78`
(`EX_CONFIG`). The supervisor treats that as sticky and stops
respawning until the frontend saves credentials and asks for a
restart.

## Frontend (`macos_frontend/`)

Intentionally thin:

- polls the daemon every 30s
- renders the menubar title: value + trend arrow, or a status glyph
  (`Connecting…`, `⚠ Offline`, `⚠ Sign In`, `⚠ Error`)
- renders a compact 12h graph in the dropdown
- provides `Refresh`, `Open at Login`, `Sign In/Out of LibreView
  Account`, `Quit`

Credentials UX is driven by native AppKit `NSPanel` modals
(first-run prompt and the account menu share the same dialog). On
save, the frontend writes the Keychain entries and asks the
supervisor to restart the daemon so it re-authenticates immediately.

The frontend must launch from inside `Gluco.app` (either the
source wrapper bundle or the standalone `py2app` bundle). Running
`python -m macos_frontend`
directly is intentionally rejected — AppKit modals need a real
`CFBundleIdentifier` and `LSUIElement` to behave.

## HTTP Contract (Localhost Only)

- `GET /health` — 3-bucket status, uptime, last error, failure count
- `GET /glucose/current` — latest reading + status + ages
- `GET /glucose/graph?hours=N` — cached history, oldest first
- `GET /patient` — first/last name + patient id
- `POST /auth/refresh` — drop session, re-auth, poll once

This is the portability seam. A future Linux frontend consumes the
same endpoints; the daemon does not change.

## State and Storage

Default state dir: `~/.glucobar/` (override via `DAEMON_STATE_DIR`).

- `daemon.lock` — single-instance guard
- `session.json` — cached session token metadata (mode 0600)
- `daemon.log` (+ rotations) — daemon application log (1 MB × 3)
- `daemon.stdout.log` / `daemon.stderr.log` — supervisor-captured
  child stdio
- `launchd.frontend.out.log` / `launchd.frontend.err.log` —
  launchd capture when running via `scripts/install_launchagent.sh`

Credentials are never read from `.env` or environment variables.
Keychain (`service = "glucobar"`) is the only source of truth, and
a partially-seeded Keychain is treated as absent.

## Configuration Surface

Non-credential config comes from `.env` / environment (see
`.env.example`). The most relevant knobs:

- `LIBREVIEW_REGION` — cluster (`US`, `EU`, `EU2`, `DE`, `FR`, `JP`, …)
  or a country alias (`PT`/`ES`/`GB` → `EU`)
- `DAEMON_URL` — override the local daemon URL
- `DAEMON_STATE_DIR`, `LOG_LEVEL`
- `ERROR_BACKOFF_*`, `RATELIMIT_BACKOFF_*`

Hardcoded runtime defaults:

- daemon poll: 60s
- frontend poll: 30s
- stale cutoff: 360s
- color thresholds (mg/dL): `<70`, `70-180`, `180-250`, `>250`

## Tests

`tests/` (pytest) covers the pieces with real logic:

- backoff behavior
- Keychain credentials contract (partial state = absent)
- daemon service state transitions
- session store persistence
- supervisor lifecycle (external vs. managed, watchdog, EX_CONFIG
  stickiness)
- parent-death pipe — daemon self-terminates when the frontend dies
  without cleanup

UI rendering is deliberately lightweight and validated by running
the app.

## Design Principles

- Abbott/LibreView complexity stays in `daemon/`.
- The frontend is thin and API-driven.
- Keychain-only credentials; missing creds is a config state, not a
  retry loop.
- Reliability > feature breadth. Gluco is a personal utility, not
  a medical workflow.

## Future Direction

Kept intentionally small:

1. Preserve the daemon API and keep auth/backoff paths robust.
2. Tighten release automation; revisit signing/notarization only if
   broad distribution becomes a goal.
3. A Linux (Fedora KDE) frontend remains possible via Qt6/PySide6
   consuming the same daemon API — `systemd --user` would replace
   the macOS supervisor. This is an explicit *frontend* port, not a
   daemon fork.

Explicit non-goals, for now:

- Multi-user / multi-patient support
- In-app threshold editing
- Desktop alerting as a headline feature
- Broad distro/package parity before the macOS path is solid

## Version and legal metadata

The release version is the `version` field in `pyproject.toml` (for example
`1.0.0`). The menubar About window and the daemon’s OpenAPI `version` read
that value via `importlib.metadata` after install. The thin `.app` bundle and
`py2app` dist bundle copy `CFBundleVersion` / `CFBundleShortVersionString` from
the same number in `Info.plist` templates / `build_portable_app.sh` — keep
those aligned when you cut a release.

User-facing copy, `NOTICE`, and `ATTRIBUTIONS.md` summarize licensing and
third-party material; the canonical Apache-2.0 text is `LICENSE`.
