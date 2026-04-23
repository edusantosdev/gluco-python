# Engineering Overview

This document explains how the app is architected and how the pieces fit
together at runtime. For a user-facing quick-start, see `README.md`. For the
full design journal (decisions, trade-offs, phased rollout), see
`glucose-menubar-implementation-plan.md`.

## 1. What the app does

The app shows the current LibreView glucose reading (value + trend arrow)
in the macOS menubar, plus a small history graph and threshold-based
notifications. It authenticates against Abbott's LibreView cloud via
`pylibrelinkup`, polls for readings, and surfaces them locally.

The design is deliberately **personal-tool-grade**: everything runs on
`127.0.0.1`, there is no multi-user story, no signing/notarization, no
telemetry. It's two Python processes (one supervising the other) and
a JSON file.

## 2. Process topology

The menubar frontend supervises the daemon as a child process; both
still communicate exclusively over local HTTP:

```
  launchd (gui/<uid>)
       │ RunAtLoad + KeepAlive
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Glucobar.app  ·  com.glucobar.frontend                              │
│                                                                      │
│  ┌─────────────────────────────────┐                                 │
│  │  macos_frontend  (rumps)        │                                 │
│  │  ├── DaemonClient (httpx) ──────┼──── GET /glucose/current ─┐     │
│  │  │                              │     GET /glucose/graph    │     │
│  │  │                              │     POST /auth/refresh    │     │
│  │  ├── GlucoseGraphView (NSView)  │                           │     │
│  │  └── NSPanel UI                 │                           │     │
│  │                                 │                           │     │
│  │  DaemonSupervisor ─ spawn ─▶ child process ─────────────────┼─┐   │
│  │     ├── /health probe           │                           │ │   │
│  │     ├── watchdog + respawn      │                           │ │   │
│  │     └── SIGTERM on quit         │                           │ │   │
│  └─────────────────────────────────┘                           │ │   │
│                                                                │ │   │
│  ┌─────────────────────────────────────────────────────────────┘ │   │
│  │  daemon (supervised child)                                    │   │
│  │  uvicorn + FastAPI on 127.0.0.1:8765  ◀──────────────────────┘   │
│  │  ├── GlucoseService (thread)                                     │
│  │  │   ├── pylibrelinkup poll                                      │
│  │  │   ├── auth + session cache                                    │
│  │  │   ├── backoff state machine                                   │
│  │  │   └── DaemonState snapshot                                    │
│  │  └── fcntl.flock on ~/.glucobar/daemon.lock                      │
│  └──────────────────────────────────────────────────────────────────┘
└──────────────────────────────────────────────────────────────────────┘
```

### Why two processes?

- The daemon owns the network-facing client and all Abbott-specific state.
  It can crash, be killed, or be restarted without taking the menubar icon
  with it — the supervisor brings it back and the menubar fades through
  `Connecting…` during the gap.
- The frontend owns AppKit. AppKit is painful to share across restarts
  and demands a main-thread runloop; keeping it isolated from the poll
  loop means HTTP retries, backoff, and session refreshes never block
  the UI.
- The HTTP seam keeps the frontend language-agnostic. A future Swift
  or KDE Plasma frontend can talk to the same `127.0.0.1:8765` with
  no daemon changes — on Linux the supervisor's job will be done by
  `systemd --user` instead (see `kde-fedora-implementation-plan.md`).

### Why one LaunchAgent + a supervisor?

Prior to 0.3.1 each process had its own LaunchAgent
(`com.glucobar.daemon` and `com.glucobar.frontend`). That worked but
had two rough edges: `launchctl kickstart` was the only way to nudge
the daemon on credential change, and a user who quit the menubar
often ended up with a "ghost daemon" still polling in the background.

From 0.3.1 onward only the frontend is registered with `launchd`, and
it supervises the daemon itself via
`macos_frontend.daemon_supervisor.DaemonSupervisor`:

- `start()` probes `${DAEMON_URL}/health` first. If something else is
  already listening (dev workflow, or the user running the daemon
  under their own supervisor) the supervisor records **external**
  mode and stays passive.
- Otherwise it spawns `sys.executable -m daemon` in its own process
  group with stdio redirected to `~/.glucobar/daemon.{stdout,stderr}.log`,
  then waits for `/health` to come up (**managed** mode).
- A watchdog thread observes the child. Regular crashes are respawned
  with a small delay and a crash-rate limit; exit code 78
  (`EX_CONFIG` — the daemon's "no credentials" signal) is treated as
  sticky and only cleared by an explicit `restart()` (e.g. after the
  user saves credentials via the menu).
- Quitting the menubar SIGTERMs the child's process group, waits,
  then SIGKILLs if needed. `atexit` covers the rumps → `NSApplication
  terminate:` path.
- A **parent-death pipe** catches the cases `atexit` can't. Python
  `signal` handlers and `atexit` hooks both get skipped when AppKit
  yanks the process out from under the interpreter (most importantly
  on `launchctl bootout`, which delivers a SIGTERM that the AppKit
  runloop often eats before Python's signal flag is checked). The
  supervisor opens an anonymous pipe at spawn time, passes the read
  end into the child via `pass_fds` and the `GLUCOBAR_PARENT_PIPE_FD`
  env var, and keeps the write end for the lifetime of the frontend.
  The daemon's `daemon.parent_watch` module blocks on that fd in a
  daemon thread; when the kernel closes the write end (for any
  reason — clean exit, SIGTERM, SIGKILL, panic), the read returns
  EOF and the watcher SIGTERMs itself, triggering the daemon's
  normal graceful-shutdown path. This is pure POSIX; on Linux the
  same code is inert because systemd never sets the env var (systemd
  drives daemon lifecycle itself).
- `bootstrap_agent` explicitly `launchctl enable`s the label before
  `launchctl bootstrap`. launchd remembers a per-label "disabled"
  flag across bootouts; without the enable step a fresh install
  fails with the infamous `5: Input/output error` if the label was
  ever bootout'd before (which happens on every reinstall).

`scripts/install_launchagent.sh` renders one plist from
`scripts/launchagents/com.glucobar.frontend.plist.tmpl` with fully
qualified paths (launchd has no useful `$PATH`), then `launchctl
bootstrap`s it into `gui/$(id -u)`. `RunAtLoad` handles login; a
crashing menubar is not `KeepAlive`d because a loop of failing
AppKit setup is worse than a dead icon. The frontend plist points at
`build/Glucobar.app`, a thin wrapper bundle whose
`Contents/MacOS/Glucobar` is a symlink to `.venv/bin/python` — this
gives AppKit a real `CFBundleIdentifier` and `LSUIElement`, which it
needs for native alerts and notifications to behave.

The topology is strictly single-wrapper / single-LaunchAgent: no
standalone `com.glucobar.daemon` plist is shipped or installed, and
there is no opt-out env var for the supervisor. Upgrades from older
versions (which did install a separate daemon LaunchAgent) are
handled by `install_launchagent.sh`, which boots out and deletes any
leftover `com.glucobar.daemon` / `com.glucose-display.*` plists so two
daemons can never race the same LibreView session.

## 3. The daemon (`daemon/`)

### Entry points

- `daemon/__main__.py` — `python -m daemon`, loads settings and hands
  off to uvicorn.
- `daemon/main.py` — module-level glue (lockfile, logging, service
  build) and the FastAPI app.

Import-time work in `main.py` is intentional: credentials, logging
config, and the single-instance lock all run **before** uvicorn binds
the socket, so failure modes (missing creds, another daemon running)
produce clean stderr output and a distinct exit code (`78` =
`EX_CONFIG`), rather than a half-started server.

### Concurrency model

- `uvicorn` runs the FastAPI event loop on the main thread.
- `GlucoseService` runs a **dedicated daemon thread** (`glucose-poller`)
  that owns `pylibrelinkup` (which is synchronous).
- The two communicate through `DaemonState`, a dataclass guarded by
  `threading.Lock`.
- FastAPI handlers are `async` but only touch the lock-guarded
  snapshot, never the network, so request latency is independent of
  poll cadence.

```
                ┌────────────────┐
 main loop ---> │  FastAPI       │ --- async reads ---> snapshot()
                │  handlers      │                           │
                └────────────────┘                           │
                                                             │ (threading.Lock)
                ┌────────────────┐                           │
 poller    ---> │ GlucoseService │ --- mutations ----------> │
 thread         │   ._tick()     │                           │
                └────────────────┘                           ▼
                                                    DaemonState dataclass
                                                    - status: Status
                                                    - reading, fetched_at
                                                    - history: list[(ts, mg_dl)]
                                                    - last_error, failures
```

### State machine (`daemon/state.py`)

`Status` is a `str`-Enum so it's JSON-serializable with no custom
encoder:

- `starting` — process up, no reading yet (HTTP 503 on `/glucose/current`).
- `ok` — fresh reading.
- `stale` — last reading older than `STALE_AFTER_SECONDS`.
- `warming_up` — sensor activated but still inside its 60-minute
  warm-up (`sensor.a + sensor.w*60 > now`).
- `sensor_ended` — sensor expired.
- `no_network` — transport errors; the daemon is backing off.
- `auth_failed` — credentials rejected.
- `tos_required` — Terms / Privacy Policy acceptance required on the
  official Libre app.
- `rate_limited` — honoring a server `Retry-After`.

`http_status_summary()` maps those nine states to a 3-bucket
(`ok | degraded | down`) summary for `/health` consumers that just
want a traffic light.

### The poll loop

`GlucoseService._tick()` in `daemon/service.py` is one iteration:

1. If `Status.TOS_REQUIRED`, retry auth every 5 minutes (the user has
   to click accept in the Libre app; hammering doesn't help).
2. If not authenticated, `_authenticate_once`. On failure, return the
   error-backoff delay.
3. Call `client.read(patient)` (the deprecated but still-only way to
   get the full `GraphResponse` with connection + sensor metadata;
   `graph()` strips the connection envelope we need for warm-up
   detection).
4. Classify the result:
   - Empty `graph_data` + active sensor metadata → `warming_up`.
   - No reading at all → `stale`.
   - Reading present → `ok` or `stale` based on the reading's
     `factory_timestamp` age.
5. Normalize `graph_data` into `(utc_ts, mg_dl)` tuples sorted
   oldest-first, clamped to 12 hours, and **replace** the history
   list atomically (never in-place mutation — snapshots held by
   other threads stay stable).
6. Reset the relevant backoff on success; return `poll_interval`.

Failures are translated into status transitions, not raised:

- `AuthenticationError` mid-session → drop the cached JWT and retry
  once right now; only fall through to backoff if that also fails.
- `LLUAPIRateLimitError` → trust `retry_after` if provided, else use
  the rate-limit backoff (separate instance from the network backoff
  so one doesn't penalise the other).
- `TermsOfUseError` / `PrivacyPolicyError` → `TOS_REQUIRED`, sleep 5m.
- Anything else → `NO_NETWORK`, sleep per error backoff.

### Backoff (`daemon/backoff.py`)

`ExponentialBackoff` is pure: `base * factor**attempt`, capped, with
"equal" (default) or "full" jitter. `next_delay()` advances the
counter; `reset()` collapses it. Two independent instances so
network errors and rate-limits back off on separate schedules.

### Session persistence (`daemon/session_store.py`)

LibreView JWTs are valid for ~1 hour. `FileSessionStore` serializes
`{token, account_id_hash, expires_at}` as mode-0600 JSON under
`~/.glucobar/session.json`, using a tempfile-then-rename so
a crash mid-write can't corrupt. Expiry is parsed passively from
the JWT `exp` claim (no signature verification — the server still
enforces real expiry; we just want a scheduling hint). Fallback TTL
of 50 minutes if the token isn't parseable.

On startup, a valid session short-circuits the password submission;
the daemon still fetches the patient list to verify the JWT, and
re-authenticates if that call returns `AuthenticationError`.

### Single-instance lock (`daemon/single_instance.py`)

An exclusive `fcntl.flock` on `~/.glucobar/daemon.lock`
prevents two daemons racing the same LibreView account (which
reliably trips Abbott's rate limiter). Held at module-scope so the
fd isn't GC'd; kernel releases it on process exit.

### HTTP API

All endpoints read from the lock-guarded snapshot. No endpoint makes
an outbound Abbott call — `/auth/refresh` does, but only to the same
service method the poller uses, running on the caller's thread.

| Method | Path                | Purpose                                             |
| ------ | ------------------- | --------------------------------------------------- |
| GET    | `/health`           | 3-bucket status + uptime + last error + failures    |
| GET    | `/glucose/current`  | Current reading (value, trend, age, status)         |
| GET    | `/glucose/graph`    | Cached history, `?hours=N` (trimmed client-side)    |
| GET    | `/patient`          | First/last name + patient id                        |
| POST   | `/auth/refresh`     | Drop session, re-authenticate, poll once            |

Response schemas live in `daemon/models.py` — centralized so they
can be reused by future SDKs and picked up by FastAPI's OpenAPI
output.

## 4. The frontend (`macos_frontend/`)

### Entry points

- `build/Glucobar.app` or `dist/Glucobar.app` launches the frontend.
- `macos_frontend/app.py` — `GlucoseApp(rumps.App)` is the UI class.

### What it knows

Essentially nothing about LibreView. It consumes the daemon's
`Status` enum as strings and maps them to menubar glyphs. Credentials
only live in the Keychain — both the **LibreView Account** menu flow
and the first-run onboarding modal (popped from `_on_first_run_tick`
when the Keychain is empty at launch) go through the same
`_native_credentials_dialog()` NSPanel with stacked email + password
fields, then call `glucose_common.credentials.save_credentials()` and
`macos_frontend.daemon_supervisor.request_restart()` to nudge the
running daemon. In managed mode that SIGTERMs the child so the
watchdog respawns it; in external mode it logs and no-ops, since
Glucobar never owns a daemon launchd job to kickstart.

The first-run prompt is deferred to a one-shot `rumps.Timer` fired
~300ms after the runloop starts rather than blocking inside
`main()`. Reason: `NSApp.runModalForWindow_` needs a live
`NSApplication` pumping events — calling it before `app.run()` hangs
or returns 0 immediately. The timer callback is the earliest
main-thread moment the modal renders cleanly.

### Tick loop

`rumps.Timer(self._on_tick, poll_interval)` fires every
`FRONTEND_POLL_SECONDS` (default 30s). Each tick:

1. `DaemonClient.current()` → `CurrentReading`.
2. Split the title into `(value, arrow, trailing)` via
   `_title_parts`:
   - `ok` → `("124", " ↗", "")`.
   - `stale` with a last value → `("124", " ↗", "*")`.
   - Everything else → a short glyph from `_STATUS_GLYPH` in the
     `value` slot with the other two empty.
3. Paint the three runs into the status button's
   `attributedTitle` via `_render_title_parts` (rumps'
   `self.title = …` only handles plain text). Thresholds are in
   mg/dL, from env (`THRESHOLD_*_MG_DL` via
   `ColorThresholds.from_env()`), and are applied to
   `value_in_mg_per_dl` so mmol/L users still get correct colours.
4. When the current arrow is directional (`↓ ↘ ↗ ↑`), a faster
   `rumps.Timer` (10 fps) runs `_on_arrow_pulse_tick`, fading the
   value + arrow together under a cosine envelope; the sideways
   `→`, the trailing `*`, and error glyphs sit still. Changing the
   arrow resets the pulse phase so transitions enter at full alpha.
5. Refresh the `Refresh` menu row's attributed title to `"Refresh
   (Updated now)"` or `"Refresh (3 min ago)"`, recomputed live
   against the last sensor timestamp rather than the daemon's
   pre-baked `reading_age_seconds`.
6. Call `/glucose/graph` and push the points into
   `GlucoseGraphView.setPoints_()`.

Transport failures show as `⚠ Offline` / `⚠ Error` in the menubar
title — the only states the daemon can't report about itself. A
`DaemonNotReady` (connection refused) collapses to `Connecting…`, the
same family as the daemon's own `starting` state.

### The dropdown graph (`macos_frontend/graph_view.py`)

A subclass of `NSView` attached to a `rumps.MenuItem` via
`NSMenuItem.setView_`. Pinned y-axis (40–300 mg/dL) so the shape
reads consistently across glances. Faint threshold bands, one
polyline, a coloured dot on the most recent sample. No interaction.
Draw happens on the main thread; `setPoints_` calls
`setNeedsDisplay:`, which AppKit documents as thread-safe.

### Dialogs — bundle-only UX

Frontend dialogs are native-only and require app-bundle launch.
`main()` calls `_ensure_bundled_launch()` and rejects direct
`python` execution.

Both the credentials prompt (`_native_credentials_dialog`) and the
informational one-button alert (`_native_info_alert`) are `NSPanel`s
rather than `NSAlert`s because `NSAlert` always reserves space for the
app icon — until we ship a real `Glucobar.icns`, that slot paints the
generic Python launcher glyph. The credentials dialog is a single
email+password window, reused for both the **LibreView Account** menu
item and the first-run onboarding panel — the latter overrides the
dialog's `title` / `body_text` / `save_button` parameters for welcome
framing.

The dialog flow runs on a background thread (`_run_in_background`)
so the rumps menu callback returns immediately and the menu doesn't
stay wedged in its tracking session. The actual AppKit calls are
marshalled back to the main thread via
`performSelectorOnMainThread:withObject:waitUntilDone:` (see the
`_MainThreadRunner` NSObject proxy) — that's the blessed PyObjC idiom
for "run this on main, wait for it, return its value".

### Menu actions

The dropdown is built in `GlucoseApp.__init__` and is intentionally
minimal: an embedded graph view row, a Refresh row, a separator, a
LibreView Account row, and rumps' standard Quit. The menubar title
carries the full status story (number, trend arrow, or an error
glyph), so there are no text status rows in the dropdown. The
`Refresh` row also doubles as the freshness readout via a dim
parenthesised suffix recomputed each time the menu opens
(`_update_refresh_item`).

Clicking the menubar pill routes through
`_StatusItemMenuPresenter.showGlucoseMenu_` rather than rumps'
default status-item-menu wiring, so the dropdown's leading edge can
be pinned to the pill's leading edge (the default AppKit anchor
uses the trailing edge and looks disconnected from a variable-width
numeric title).

| Item                       | Behaviour                                                                 |
| -------------------------- | ------------------------------------------------------------------------- |
| Refresh (freshness hint)   | `DaemonClient.force_refresh()` → `POST /auth/refresh` on the daemon, then `_on_tick(None)` (`_session_refresh_then_tick` in `app.py`) so LibreView is re-authenticated and the title / menu / graph refresh together. |
| LibreView Account          | Native `NSPanel` modal → `save_credentials()` → Keychain → `daemon_supervisor.request_restart()` (SIGTERMs the supervised child, watchdog respawns). Same `_native_credentials_dialog()` is reused by the first-run onboarding modal with welcome-flavoured copy. |
| Quit                       | Standard rumps quit.                                                      |

## 5. Shared code (`glucose_common/`)

Four small modules the daemon, frontend, and scripts all import:

- `credentials.py` — Keychain-only resolver (`load_credentials`),
  `save_credentials`, `clear_credentials`. One service name
  (`glucobar`; was `librelinkup-glucose-menubar` pre-0.3), two
  usernames (`email`, `password`) so both secrets sit in one
  Keychain-visible group. A partially-seeded Keychain (one half
  present, the other missing) is treated as absent — silently
  combining with anything else would hide bugs.
- `regions.py` — `LIBREVIEW_REGION` → `pylibrelinkup.APIUrl`, with
  country-code aliases (`PT`/`ES`/`GB`/… → `EU`).
- `trend.py` — integer `Trend` code → `↑↗→↘↓` glyph or
  `rising_fast`/`rising`/… name. Kept free of `pylibrelinkup` so the
  frontend can import it without pulling the LLU client in.
- `units.py` — `glucose_units` integer → `"mg/dL"` / `"mmol/L"`
  label.

## 6. Configuration

`daemon/config.py` defines a `pydantic-settings` `Settings` model.
Environment variables + `.env` feed the non-credential knobs (region,
poll interval, backoff…); `load_settings()` then pulls email/password
straight from the Keychain via `glucose_common.credentials` and fails
loudly with `MissingCredentialsError` if the Keychain has none. There
is no env/`.env` fallback for credentials — if the daemon starts
without Keychain entries it exits `78` (`EX_CONFIG`). Under the
supervisor that's sticky (see section 2): the menubar prompts the
user, saves to Keychain, and calls `request_restart()` to clear the
sticky flag and relaunch the child.

State lives under `DAEMON_STATE_DIR` (default
`~/.glucobar/`):

```
~/.glucobar/
├── daemon.lock             # fcntl flock sentinel
├── session.json            # JWT + account_id_hash (mode 0600)
├── daemon.stdout.log       # supervised child stdio (append)
├── daemon.stderr.log       # supervised child stdio (append)
├── launchd.frontend.*.log  # launchd's capture of the menubar process
└── logs/daemon.log*        # RotatingFileHandler (1 MB × 5)
```

Frontend settings are plain `os.environ` reads — no pydantic, because
the frontend has very few knobs and they're all optional:

- `DAEMON_URL`, `FRONTEND_POLL_SECONDS`
- `THRESHOLD_LOW_MG_DL`, `THRESHOLD_HIGH_MG_DL`, `THRESHOLD_VERY_HIGH_MG_DL`

There's no notification subsystem — an earlier iteration shipped a
`macos_frontend/notifier.py` with threshold/hysteresis alerts, but it
was removed along with the corresponding `NOTIFY_*` env vars. The
menubar title carries all status signal; adding desktop notifications
is not a current goal (see section 10).

## 7. Testing (`tests/`)

Unit tests (pytest) cover the pieces that benefit most:

- `test_backoff.py` — deterministic with a seeded RNG
  (`seed_for_tests`).
- `test_credentials.py` — fake `KeyringLike`, verifies the
  Keychain-only resolver and the "partial state is treated as absent"
  rule.
- `test_service_state.py` — stubs `pylibrelinkup` with hand-built
  fake responses to cover warm-up, stale, fresh, and error paths.
- `test_session_store.py` — round-trip + atomic-write behaviour.
- `test_daemon_supervisor.py` — external-vs-managed mode detection,
  watchdog crash-restart, EX_CONFIG sticky-state handling,
  `request_restart()` behavior (SIGTERM the managed child, no-op
  otherwise — there is no launchctl fallback in the strict topology),
  and the `GLUCOBAR_PARENT_PIPE_FD` / `pass_fds` wiring for the
  parent-death pipe. Uses DI hooks so no real subprocess is ever
  spawned.
- `test_parent_watch.py` — verifies the daemon-side half of the
  parent-death pipe: absent env var is a no-op, bogus values are
  ignored rather than raised, closing the pipe's write end fires a
  SIGTERM at ourselves (with `os.kill` monkeypatched so pytest
  survives the test).

PyObjC / AppKit code (graph view, dialogs) is not unit-tested —
it's exercised by running the app, and deliberately kept thin so
there isn't much logic to cover.

## 8. Data flow — one successful tick, end-to-end

1. Every `POLL_INTERVAL_SECONDS` (60s default), the poller thread
   calls `pylibrelinkup.read(patient)`.
2. Response is folded into `DaemonState`: status, current
   measurement, history window, `fetched_at`, failures reset.
3. Every `FRONTEND_POLL_SECONDS` (30s default), the rumps timer on
   the main thread fires `_on_tick`.
4. `DaemonClient.current()` → HTTP GET `/glucose/current` →
   FastAPI handler → `_service.snapshot()` → `CurrentResponse`.
5. Frontend formats the title (integer + arrow), colours it through
   an `NSAttributedString`, and updates the status menu rows.
6. `DaemonClient.graph(hours=8)` → `/glucose/graph` → the same
   snapshot's `history` list, filtered to the last 8 hours →
   `GlucoseGraphView.setPoints_(...)` → `setNeedsDisplay:` →
   `drawRect:` on the next runloop pass.

That's the whole loop.

## 9. Failure modes worth knowing

- **Daemon down, frontend up** → menubar shows `⚠ Offline` (or
  `Connecting…` if the socket is merely not bound yet, i.e.
  `DaemonNotReady`). The supervisor's watchdog respawns the child
  within a couple of seconds; opening the menu triggers an immediate
  retry (`showGlucoseMenu_` → `_on_tick`); otherwise wait for the
  next `FRONTEND_POLL_SECONDS` tick.
- **Network down** → daemon flips to `no_network`, the frontend
  shows `⚠ Offline`, and the error backoff escalates from 10s to
  300s (default cap).
- **JWT expired mid-session** → one inline auth retry; only falls
  through to backoff if that retry also fails.
- **ToS / Privacy Policy change** → poll loop parks at a 5-minute
  cadence so the user can accept in the Libre app and recovery
  happens automatically on the next tick.
- **Two daemons started** → second one exits with `Refusing to
  start a second instance` and exit code 1, before binding a
  socket. First one keeps running.
- **Missing creds at boot** → daemon exits 78 (`EX_CONFIG`) with a
  stderr message. The supervisor marks this as a sticky config error
  (no auto-respawn) so we don't fork-bomb launchd's process table
  while the user is AFK. On first launch the menubar detects the
  empty Keychain, prompts the user, saves, and calls
  `request_restart()` which clears the sticky flag and lets the
  watchdog spawn a fresh daemon with the new credentials.
- **Frontend launched outside `Glucobar.app`** → startup fails fast via
  `_ensure_bundled_launch()` with an explicit "unsupported launch mode"
  error.

## 10. Non-goals

The plan explicitly excludes:

- Multi-user / multi-patient support (first patient in the list
  wins).
- A Linux port in-tree (the frontend is AppKit-only; the daemon is
  already portable but there's no Linux UI here yet — see
  `kde-fedora-implementation-plan.md` for the design sketch).
- Menu-driven threshold editing — colours and graph bands use
  `THRESHOLD_*_MG_DL` from the environment only.
- Desktop notifications for hypo/hyper thresholds — the previous
  `notifier` module was removed in favour of the always-visible
  menubar title.
