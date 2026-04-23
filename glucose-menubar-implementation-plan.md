# Glucobar — Design Journal

A retrospective on how the project was planned and what actually
shipped. For the user-facing tour see [`README.md`](./README.md); for
the wiring as it exists today see [`ENGINEERING.md`](./ENGINEERING.md).
For the forward-looking Linux port see
[`kde-fedora-implementation-plan.md`](./kde-fedora-implementation-plan.md).

This document exists to answer "why was it built this way?" rather
than "how does it work today?" — the former ages well, the latter
belongs in `ENGINEERING.md`.

---

## 1. Goals

The project surfaces a single piece of information — current glucose
value with trend arrow — in a persistent, always-visible,
zero-interaction way. No phone unlock, no app switch, no sensor scan.
Just a glance at the menubar.

Secondary goals that shaped the design:

- **Run for months without intervention.** Survive macOS sleep/wake,
  network blips, Abbott's periodic forced re-authentication, and
  sensor warm-up without crashing or requiring a restart.
- **Port cleanly.** The same glucose data is useful on a workstation
  regardless of OS, so the architecture should make a Linux (and
  eventually a Swift) frontend a reskin, not a rewrite.
- **Stay a personal utility.** Not a medical device, not a dosing
  aid, not a replacement for the official Libre app, not
  multi-user. Every design call defers to that framing.

---

## 2. Architecture — three layers, stable seams

The core insight: **put all Abbott-facing complexity in one Python
daemon with a stable local HTTP interface, and every UI frontend in
every language becomes a dumb consumer of that interface.**

```
┌─────────────────────────────────────────────────────┐
│ Layer 3: UI Frontend                                │
│  macOS: Python + rumps + AppKit (shipped)           │
│  Linux: Python + PySide6 (planned — see KDE plan)   │
│  Swift: MenuBarExtra (hypothetical future)          │
│                                                     │
│  HTTP over 127.0.0.1                                │
└────────────────────┬────────────────────────────────┘
                     │ GET /glucose/current
                     │ GET /glucose/graph
                     │ POST /auth/refresh
                     ▼
┌─────────────────────────────────────────────────────┐
│ Layer 2: Data Daemon                                │
│  Python + FastAPI + Uvicorn                         │
│  Handles: auth, polling, caching, backoff, state    │
│  Exposes: localhost REST API                        │
└────────────────────┬────────────────────────────────┘
                     │ client.read(patient)
                     ▼
┌─────────────────────────────────────────────────────┐
│ Layer 1: LibreView Client                           │
│  pylibrelinkup                                      │
│  Talks HTTPS to Abbott's LibreView servers          │
└─────────────────────────────────────────────────────┘
```

The daemon is the single point where Abbott complexity lives — token
caching, rate-limit backoff, region selection, ToS re-acceptance,
sensor warm-up, `EX_CONFIG` semantics. When Abbott breaks something
(and they do), there is exactly one place to fix it.

The daemon binds to `127.0.0.1` only, no auth, no TLS. It is a
personal tool on a trusted machine, not a service. Loopback-only is
sufficient isolation.

### One LaunchAgent + a supervisor (post-0.3.1)

The original plan called for two separate LaunchAgents
(`com.glucobar.daemon` and `com.glucobar.frontend`), each with
`KeepAlive=true`. That worked but produced two rough edges:

- After the user quit the menubar, the daemon kept polling — a
  "ghost daemon" holding `daemon.lock` and burning rate-limit budget.
- Credential changes had to be signalled with `launchctl kickstart`,
  which is fine on the CLI and awful from AppKit.

The shipped model is **one LaunchAgent for the frontend**, which
supervises the daemon as a child process via
`macos_frontend.daemon_supervisor.DaemonSupervisor`. The supervisor:

- Probes `${DAEMON_URL}/health` first and stays passive in
  **external** mode if something else is already listening (dev
  workflow).
- Otherwise spawns `python -m daemon` in its own process group and
  watches it (**managed** mode), respawning on crash with a
  crash-rate limit.
- Treats `EX_CONFIG` (exit 78 = "no credentials") as sticky: no
  auto-respawn until the menubar saves credentials and calls
  `request_restart()`.
- Tears the child down via `SIGTERM` on Quit, escalating to
  `SIGKILL` if needed.
- Detects its own death via a **parent-death pipe** — an anonymous
  pipe whose read end lives in the child (`GLUCOBAR_PARENT_PIPE_FD`)
  and whose write end is held by the frontend. When the frontend
  dies for any reason (clean exit, SIGKILL, AppKit yanking the
  interpreter out from under `atexit`, `launchctl bootout`), the
  daemon's `daemon.parent_watch` thread sees EOF and SIGTERMs
  itself. Pure POSIX, inert on Linux (where `systemd --user` does
  the same job).

This keeps the HTTP seam — a future KDE/Plasma frontend talks to
`127.0.0.1:8765` with no daemon changes, and on Linux the
supervisor's job is simply handed to `systemd --user`.

---

## 3. Tech stack

**Layer 1 — LibreView client:** `pylibrelinkup`. Actively
maintained, already handles Abbott's auth flow (privacy-policy and
ToS errors, `Account-Id` header hash, region URLs). Known gaps are
papered over in `daemon/session_store.py` until upstream adds session
persistence.

**Layer 2 — Daemon:** Python 3.11+, FastAPI, Uvicorn. FastAPI gives
typed JSON responses and auto-generated OpenAPI docs for free;
Uvicorn runs the event loop. Pydantic-settings feeds non-credential
config from environment variables; credentials are Keychain-only.

**Layer 3 — macOS frontend:** Python 3.11+, `rumps` (thin wrapper
over `NSStatusItem`) + direct PyObjC / AppKit for modals and the
dropdown graph. `rumps` was the fastest route to a working icon; the
AppKit escape hatches were only needed for things rumps can't do
cleanly (per-run text colours, stacked modal fields, dropdown
custom views).

**Layer 3 — Linux frontend (planned):** PySide6 + QSystemTrayIcon,
detailed in the KDE plan.

**Packaging — macOS:** a thin wrapper `.app` bundle rather than a
`py2app` / `briefcase` freeze. `build/Glucobar.app/Contents/MacOS/
Glucobar` is a symlink to `.venv/bin/python`, plus a copy of
`pyvenv.cfg` and a `site-packages` symlink one level up — enough to
give AppKit a valid `CFBundleIdentifier` and `LSUIElement=true`
without an interpreter freeze or a rebundle-on-every-edit loop. The
same trick makes `NSPanel.runModalForWindow_` actually work; direct
terminal launches are intentionally unsupported, and the frontend now
fails fast outside `Glucobar.app`.

A real `py2app` build producing a redistributable `Glucobar.app` is
on the packaging roadmap in `README.md` but not shipped yet.

**Dev tooling:** `uv` for env management, `ruff` for lint + format,
`pytest` for tests. All boring, all intentional.

---

## 4. Phased roadmap — what shipped

The work was organised so that each phase produced something usable.
The daemon boundary was deferred to Phase 2 (not Phase 1) because
the MVP's job was to validate the product at all — separation of
concerns could wait one phase.

### Phase 0 — Verify the API (done)

`scripts/phase0_probe.py` still exists: loads `.env`, authenticates,
fetches the first patient's latest reading, prints JSON. Run it
before reporting "Glucobar doesn't work" — it isolates account /
region / ToS problems from everything else.

### Phase 1 — Monolithic MVP (done, removed)

A single `mvp.py` imported `pylibrelinkup` directly and polled in a
`rumps.Timer` callback. It proved the concept in an evening and was
retired the moment Phase 2 landed. The only Phase 1 artifact that
survives in-tree is `glucose_common/trend.py` — the trend-arrow map
was placed in the shared package from day one so the split would be
a move, not a duplication.

### Phase 2 — Extract the daemon (done)

The monolith became the three-layer architecture: `daemon/` (FastAPI
+ poller thread + status enum + HTTP contract) and `macos_frontend/`
(rumps app consuming the daemon over HTTP). The API contract lives
in `daemon/models.py` today; the shipped surface is:

| Method | Path              | Purpose                                     |
| ------ | ----------------- | ------------------------------------------- |
| GET    | `/health`         | 3-bucket status + uptime + last error       |
| GET    | `/glucose/current`| Current reading (value, trend, age, status) |
| GET    | `/glucose/graph`  | Cached history, `?hours=N`                  |
| GET    | `/patient`        | First/last name + patient id                |
| POST   | `/auth/refresh`   | Drop session, re-authenticate, poll once    |

Two ages are exposed, not one: `reading_age_seconds` measures
staleness from the sensor's point of view (what the user cares
about) and `fetch_age_seconds` measures staleness of our own poll
(what diagnostics care about). They diverge whenever Abbott hasn't
pushed a new reading since we last fetched.

Non-`ok` states still return HTTP 200 — they're legitimate
operational states (warm-up, stale, rate-limited, ToS-required),
not errors. Only genuine daemon failure (pre-startup, cache empty)
returns 503.

### Phase 3 — Robustness (done)

The failure modes that only reveal themselves over days of real use:

- **Sensor warm-up.** 60-minute window after applying a new sensor.
  `daemon/service.py` classifies an empty `graph_data` + active
  sensor metadata as `warming_up`; the frontend shows `Connecting…`
  rather than an error glyph.
- **Token expiry.** JWTs last ~1 hour. The poll loop catches
  `AuthenticationError`, drops the cached JWT, retries once inline;
  only falls through to backoff if that second attempt also fails.
  `FileSessionStore` persists `{token, expires_at,
  account_id_hash}` as mode-0600 JSON with a tempfile-then-rename
  write so a daemon restart doesn't force a fresh login.
- **Network loss.** `ExponentialBackoff` with equal-jitter by
  default: base × factor^attempt, capped, reset on success. Jitter
  matters because laptop wake-from-sleep makes many background
  services reconnect at once — deterministic delays thundering-herd.
- **Rate limiting.** Honour `retry_after` when provided, fall back
  to a separate rate-limit backoff instance otherwise. Kept on its
  own counter so network errors and rate-limits don't penalise each
  other.
- **Abbott ToS / Privacy Policy re-accept.** Daemon can't solve this
  — user must accept in the Libre app. Poll loop drops to 5-minute
  cadence while `TOS_REQUIRED` (hammering Abbott with rejected
  requests is pointless and burns budget).
- **Daemon crashes.** Watchdog thread in the supervisor respawns
  with backoff; `EX_CONFIG` is sticky (no auto-respawn) so a
  missing-credentials loop can't fork-bomb `launchd`'s process
  table while the user is AFK.
- **Single-instance guard.** `fcntl.flock` on `~/.glucobar/daemon.
  lock` prevents two daemons racing the same LibreView account
  (which reliably trips Abbott's rate limiter).
- **Graceful shutdown.** FastAPI `lifespan` `finally` block stops
  the poller; supervisor SIGTERMs the child's process group on
  Quit; parent-death pipe catches everything else.
- **Structured logging.** `RotatingFileHandler` at
  `~/.glucobar/logs/daemon.log` (1 MB × 5). A daemon that runs for
  months without logs is un-debuggable; a daemon that grows an
  unbounded log is a different kind of broken.

### Phase 4 — macOS polish (done)

- Launch-at-login via the frontend LaunchAgent (not two agents —
  see section 2).
- Credentials in Keychain only, no env-var fallback. Frontend has a
  **LibreView Account** menu item that opens an `NSPanel` with
  stacked email + password fields; first-run onboarding reuses the
  same `NSPanel` with welcome-flavoured copy, deferred via a
  one-shot `rumps.Timer` so the AppKit runloop is live when the
  modal opens.
- Colour-coded menubar number via `THRESHOLD_*_MG_DL` env vars.
- Pulsing value + arrow on directional trends (`↓ ↘ ↗ ↑`); static on
  `→`, error glyphs, and the trailing `*` stale flag.
- Dropdown graph (`GlucoseGraphView`) — 8-hour line, pinned 40–300
  mg/dL y-axis, faint threshold bands, coloured dot on the latest
  sample.
- Thin `.app` wrapper bundle so AppKit has a real
  `CFBundleIdentifier` + `LSUIElement=true`. Without it, modal
  dialogs silently tear the launchd-spawned Python process down and
  the menu "LibreView Account" item looks like it does nothing.

Two items from earlier drafts were dropped:

- **Menu-driven threshold editing.** Thresholds stay env-only —
  simple, static, no state to persist across restarts. `.env` is
  already where the user would edit other knobs.
- **Hypo/hyper desktop notifications.** A `macos_frontend/notifier.
  py` module with threshold/hysteresis/cooldown alerts shipped
  briefly and was removed. The menubar title already reads as the
  alert surface for a personal always-visible tool — colour-coded
  number, pulsing trend arrow, `⚠ Offline` / `⚠ Sign In` glyphs.
  Reintroducing notifications is not a current goal. The Linux port
  will have to build its policy module from scratch rather than
  lifting shared code (see the KDE plan).

### Phase 5 — Linux port (deferred to KDE plan)

Not started in-tree. Design and phasing live in
`kde-fedora-implementation-plan.md`. The daemon is already
portable; the supervisor stays macOS-only because `systemd --user`
will do its job on Linux.

### Phase 6 — Swift rewrite (optional, unscheduled)

Hypothetical. Because the HTTP contract is stable, a SwiftUI
`MenuBarExtra` frontend remains a weekend of work rather than a
rescue project. Nothing is blocking it except the absence of a
clear reason to do it.

---

## 5. Configuration surface

Credentials are **Keychain-only** — no `.env` fallback, no
environment-variable fallback. A partially seeded Keychain (one
half present, the other missing) is treated as absent so silent
mis-merging can't hide bugs. Non-credential knobs still come from
`.env` / `os.environ`:

- `LIBREVIEW_REGION` (required) — cluster (`US`, `EU`, `EU2`, …) or
  a country-code alias that maps to one (`PT` / `ES` / `GB` → `EU`).
- `POLL_INTERVAL_SECONDS`, `FRONTEND_POLL_SECONDS`,
  `STALE_AFTER_SECONDS`, `DAEMON_URL`.
- `THRESHOLD_LOW_MG_DL`, `THRESHOLD_HIGH_MG_DL`,
  `THRESHOLD_VERY_HIGH_MG_DL`.
- `DAEMON_STATE_DIR` (default `~/.glucobar/`), `LOG_LEVEL`.
- `GLUCOBAR_SUPERVISE_DAEMON` (default on) — set `0` to disable the
  frontend's supervisor for power-users running the daemon under
  their own process manager.

The full list with defaults lives in `.env.example`.

State lives under `~/.glucobar/`:

```
~/.glucobar/
├── daemon.lock             # fcntl flock sentinel
├── session.json            # JWT + account_id_hash (mode 0600)
├── daemon.stdout.log       # supervised child stdio (append)
├── daemon.stderr.log       # supervised child stdio (append)
├── launchd.frontend.*.log  # launchd's capture of the menubar process
└── logs/daemon.log*        # RotatingFileHandler (1 MB × 5)
```

---

## 6. Testing strategy

Unit tests (`tests/`, pytest) cover the pieces that benefit most —
pure logic, state machines, and process orchestration. PyObjC / AppKit
code is exercised by running the app, kept deliberately thin so there
isn't much logic to cover:

- `test_backoff.py` — deterministic with a seeded RNG.
- `test_credentials.py` — fake `KeyringLike`, "partial state is
  treated as absent" rule.
- `test_service_state.py` — stubbed `pylibrelinkup` covering
  warm-up, stale, fresh, and error paths.
- `test_session_store.py` — round-trip + atomic-write behaviour.
- `test_daemon_supervisor.py` — external-vs-managed detection,
  watchdog crash-restart, `EX_CONFIG` sticky-state,
  `request_restart()` fallback, `pass_fds` / `GLUCOBAR_PARENT_PIPE_
  FD` wiring. DI hooks so no real subprocess is spawned.
- `test_parent_watch.py` — daemon side of the parent-death pipe
  (no-op without env var, bogus fd tolerated, EOF triggers
  SIGTERM).

CI never hits Abbott's servers.

---

## 7. Open questions (parked, not blocking)

- **Multiple patients / followers.** MVP assumes one patient (first
  in the list wins). A patient-selection endpoint would be cheap to
  add if someone actually asks for it; probably out of scope forever
  for personal use.
- **Offline behaviour beyond the stale window.** Staleness cuts over
  at `STALE_AFTER_SECONDS` (default 360). Beyond that the menubar
  shows `Connecting…` rather than a dangerously-old number. The
  current answer works; it might want tuning once there's more real
  usage data.
- **Alarms for hypos.** The Libre app's alarm system is local and
  phone-bound. A Glucobar-side alarm would mean reviving the
  notifier policy; the bar for doing so is "I actually missed the
  noise", not speculation.
- **Units.** Daemon returns whatever `glucose_units` the
  account uses; mg/dL thresholds are converted in the frontend
  before colouring, so mmol/L accounts still get correct colours.
  No user-facing unit toggle.

---

## 8. Summary

The architecture's core insight — **Abbott complexity in one daemon,
dumb HTTP-consuming frontends per platform** — paid off exactly as
intended. Phase 1 was an evening. Phase 2 was the weekend that
earned portability. Phases 3–4 turned the toy into something
reliable. The future Linux port (and any hypothetical Swift rewrite)
is a frontend reskin, not a rescue project.

The main design calls that survived contact with reality:

- Daemon as a child process of the frontend, supervised in-tree —
  no ghost daemons, credential changes stay in the AppKit process.
- Keychain-only credentials, `EX_CONFIG` as a sticky error.
- Parent-death pipe, because AppKit eats signals and `atexit`
  hooks are unreliable under `launchctl bootout`.
- Two separate status axes — HTTP summary for traffic-light clients,
  full `Status` enum for frontends that want nuance.

The main things that didn't survive:

- Two LaunchAgents → one LaunchAgent + supervisor.
- Menu-driven threshold editing → env-only.
- Desktop notifications → menubar title carries all the signal.
- Env-var credential fallback → Keychain only.

None of the above needs to be re-litigated. Everything else — the
HTTP contract, the status enum, the state file layout — has held up
unchanged across ~6 months of daily use.
