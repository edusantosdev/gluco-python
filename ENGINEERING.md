# Engineering Overview

This document explains how the app is architected and how the pieces fit
together at runtime. For a user-facing quick-start, see `README.md`. For the
full design journal (decisions, trade-offs, phased rollout), see
`glucose-menubar-implementation-plan.md`.

## 1. What the app does

The app shows the current LibreLinkUp glucose reading (value + trend arrow)
in the macOS menubar, plus a small history graph and threshold-based
notifications. It authenticates against Abbott's LibreView cloud via
`pylibrelinkup`, polls for readings, and surfaces them locally.

The design is deliberately **personal-tool-grade**: everything runs on
`127.0.0.1`, there is no multi-user story, no signing/notarization, no
telemetry. It's two Python processes and a JSON file.

## 2. Process topology

Two separate processes cooperate over local HTTP:

```
┌───────────────────────────────────┐       ┌─────────────────────────────────┐
│             daemon                │       │          macos_frontend         │
│  (`python -m daemon`)             │       │  (`Glucose Menubar.app`)        │
│                                   │       │                                 │
│  uvicorn + FastAPI                │       │  rumps menubar app              │
│    │                              │       │    │                            │
│    ├── GlucoseService (thread) ───┼──────▶│    ├── DaemonClient (httpx)     │
│    │   ├── pylibrelinkup poll     │       │    │    GET /glucose/current    │
│    │   ├── auth + session cache   │       │    │    GET /glucose/graph      │
│    │   ├── backoff state machine  │       │    │    POST /auth/refresh      │
│    │   └── DaemonState snapshot   │       │    │                            │
│    │                              │       │    ├── GlucoseGraphView (NSView)│
│    └── HTTP 127.0.0.1:8765        │       │    └── NSAlert / osascript UI   │
└───────────────────────────────────┘       └─────────────────────────────────┘
         ▲                                                    ▲
         │                                                    │
         │ LaunchAgent #1: com.glucose-display.daemon         │ LaunchAgent #2:
         │                                                    │ com.glucose-display.frontend
         └───────────── launchd (user session) ───────────────┘
```

### Why two processes?

- The daemon owns the network-facing client and all Abbott-specific state.
  It can crash, be killed, or be restarted without taking the menubar icon
  with it.
- The frontend owns AppKit. AppKit is painful to share across restarts
  and demands a main-thread runloop; keeping it isolated from the poll
  loop means HTTP retries, backoff, and session refreshes never block
  the UI.
- The HTTP seam keeps the frontend language-agnostic. A future Swift
  menubar could talk to the same `127.0.0.1:8765` with no daemon
  changes.

### Why `launchd`?

`scripts/install_launchagent.sh` renders two plists from
`scripts/launchagents/*.tmpl` with fully qualified paths (launchd has
no useful `$PATH`), then `launchctl bootstrap`s them into
`gui/$(id -u)`. `KeepAlive` restarts crashes; `RunAtLoad` handles
login. The frontend plist points at `build/Glucose Menubar.app`, a
thin wrapper bundle whose `Contents/MacOS/Glucose Menubar` is a
symlink to `.venv/bin/python` — this gives AppKit a real
`CFBundleIdentifier` and `LSUIElement`, which it needs for native
alerts and notifications to behave.

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
`~/.glucose-display/session.json`, using a tempfile-then-rename so
a crash mid-write can't corrupt. Expiry is parsed passively from
the JWT `exp` claim (no signature verification — the server still
enforces real expiry; we just want a scheduling hint). Fallback TTL
of 50 minutes if the token isn't parseable.

On startup, a valid session short-circuits the password submission;
the daemon still fetches the patient list to verify the JWT, and
re-authenticates if that call returns `AuthenticationError`.

### Single-instance lock (`daemon/single_instance.py`)

An exclusive `fcntl.flock` on `~/.glucose-display/daemon.lock`
prevents two daemons racing the same LibreLinkUp account (which
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

- `macos_frontend/__main__.py` → `python -m macos_frontend`
- `macos_frontend/app.py` — `GlucoseApp(rumps.App)` is the UI class.

### What it knows

Essentially nothing about LibreLinkUp. It consumes the daemon's
`Status` enum as strings and maps them to menubar glyphs. No
credentials live in the frontend process — even the **LibreLinkUp Account**
flow writes straight to Keychain via
`glucose_common.credentials.save_credentials()`.

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

### Dialogs — two-mode UX

Credential prompts split by runtime mode, controlled
by `_is_bundled()`:

- **Bundled** (launched from `Glucose Menubar.app` under launchd):
  AppKit sees the real `CFBundleIdentifier` / `LSUIElement`, so
  a modal `NSPanel` with stacked fields works (we avoid `NSAlert` here
  because it always reserves space for the app icon). The credentials
  dialog is a single email+password window.
- **Unbundled** (`python -m macos_frontend` in a terminal): AppKit
  silently tears modal-showing Python processes down a few seconds
  after `runModal()`. We fall back to two sequential `osascript`
  dialogs — `osascript` itself is a properly bundled helper, so the
  UI renders in its process, not ours.

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
LibreLinkUp Account row, and rumps' standard Quit. The menubar title
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
| Refresh (freshness hint)   | `DaemonClient.force_refresh()` → `POST /auth/refresh` on the daemon, then `_on_tick(None)` (`_session_refresh_then_tick` in `app.py`) so LibreLinkUp is re-authenticated and the title / menu / graph refresh together. |
| LibreLinkUp Account        | Native `NSPanel` modal (bundled) or sequential `osascript` prompts (unbundled) → `save_credentials()` → Keychain. |
| Quit                       | Standard rumps quit.                                                      |

## 5. Shared code (`glucose_common/`)

Four small modules the daemon, frontend, and scripts all import:

- `credentials.py` — Keychain-first, env-fallback resolver
  (`load_credentials`), `save_credentials`, `clear_credentials`. One
  service name (`librelinkup-glucose-menubar`), two usernames
  (`email`, `password`) so both secrets sit in one Keychain-visible
  group. We refuse to mix sources: partial Keychain state always
  falls through to env (silent combining hides bugs).
- `regions.py` — `LIBRELINKUP_REGION` → `pylibrelinkup.APIUrl`, with
  country-code aliases (`PT`/`ES`/`GB`/… → `EU`).
- `trend.py` — integer `Trend` code → `↑↗→↘↓` glyph or
  `rising_fast`/`rising`/… name. Kept free of `pylibrelinkup` so the
  frontend can import it without pulling the LLU client in.
- `units.py` — `glucose_units` integer → `"mg/dL"` / `"mmol/L"`
  label.

## 6. Configuration

`daemon/config.py` defines a `pydantic-settings` `Settings` model.
Environment variables + `.env` feed it; `load_settings()` then
reconciles email/password with Keychain (Keychain wins) and fails
loudly if neither source has them.

State lives under `DAEMON_STATE_DIR` (default
`~/.glucose-display/`):

```
~/.glucose-display/
├── daemon.lock      # fcntl flock sentinel
├── session.json     # JWT + account_id_hash (mode 0600)
└── daemon.log       # RotatingFileHandler
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
- `test_credentials.py` — fake `KeyringLike`, verifies precedence
  and the "never mix sources" rule.
- `test_service_state.py` — stubs `pylibrelinkup` with hand-built
  fake responses to cover warm-up, stale, fresh, and error paths.
- `test_session_store.py` — round-trip + atomic-write behaviour.

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
  `DaemonNotReady`). launchd should restart the daemon; opening the
  menu triggers an immediate retry (`showGlucoseMenu_` → `_on_tick`);
  otherwise wait for the next `FRONTEND_POLL_SECONDS` tick.
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
  stderr message; launchd records the config-error exit code
  distinctly from runtime failures.
- **Unbundled AppKit modals** → fallback to `osascript` dialogs.
  This is detected (`_is_bundled()`), not configured.

## 10. Non-goals

The plan explicitly excludes:

- Multi-user / multi-patient support (first patient in the list
  wins).
- Codesigning or notarization of `Glucose Menubar.app`.
- A Linux port in-tree (the frontend is AppKit-only; the daemon is
  already portable but there's no Linux UI here yet — see
  `kde-fedora-implementation-plan.md` for the design sketch).
- Menu-driven threshold editing — colours and graph bands use
  `THRESHOLD_*_MG_DL` from the environment only.
- Desktop notifications for hypo/hyper thresholds — the previous
  `notifier` module was removed in favour of the always-visible
  menubar title.
