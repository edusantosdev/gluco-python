# KDE Plasma (Fedora) Implementation Plan

This plan sketches the Linux port against the current codebase —
the FastAPI daemon under `daemon/`, the AppKit menubar under
`macos_frontend/`, and the shared helpers in `glucose_common/`. The
daemon is intentionally portable; the menubar is not. See
[`ENGINEERING.md`](./ENGINEERING.md) for the shipped architecture
and [`glucose-menubar-implementation-plan.md`](./glucose-menubar-implementation-plan.md)
for the design rationale this builds on.

## Plasma Compatibility Target

Primary support target is **KDE Plasma 6 (latest stable)** on Fedora KDE spins, with **Wayland as the default session**.

Compatibility baseline to enforce during implementation:

- Fedora KDE latest two stable releases (rolling target).
- Plasma 6.x latest point release on those Fedora versions.
- Qt 6 / PySide6 stack only (no new Qt5 dependency path).
- Working behavior on both Wayland and X11 sessions, but Wayland is the release gate.

## 1) Goal and Scope

Deliver a Linux desktop frontend that runs well on Fedora KDE Plasma while reusing the existing daemon contract and operational behavior.

In scope:

- Fedora + KDE Plasma as first-class target.
- Tray icon title updates, menu actions, notifications, credential updates, and threshold editing.
- Graph preview in the tray menu (equivalent to current macOS "glance graph").
- Autostart/service management suitable for Linux desktop usage.
- RPM packaging + repository publishing so install is a simple `dnf install`.

Out of scope (initially):

- GNOME-specific shell-extension UX.
- Flatpak/Snap packaging.
- Full distro matrix beyond Fedora (can be added after Fedora path is stable).

## 2) Current Architecture Review

The current layout is already favorable for a Linux UI port:

- `daemon/` is portable Python FastAPI + background poller.
- `daemon/main.py` exposes stable endpoints:
  - `GET /glucose/current`
  - `GET /glucose/graph?hours=...`
  - `POST /auth/refresh`
  - `GET /health`, `GET /patient`
- `daemon/service.py` handles auth refresh, backoff, warm-up
  detection, stale detection, and graph history caching.
- `daemon/parent_watch.py` arms a POSIX pipe-based parent-death
  watch when `GLUCOBAR_PARENT_PIPE_FD` is set. On macOS the
  supervisor passes the fd; on Linux systemd drives the lifecycle
  directly and the env var is absent, so the module is inert.
- `glucose_common/credentials.py` is Keychain-only on macOS; on
  Linux `keyring` picks up Secret Service / KWallet automatically.
  There is **no env-var fallback** for credentials — partial state
  (email present but password missing, or vice versa) is treated
  as absent.

macOS-specific pieces are concentrated in:

- `macos_frontend/app.py` (rumps + AppKit `NSPanel` modals,
  title pulse, status-menu alignment,
  first-run onboarding).
- `macos_frontend/graph_view.py` (AppKit drawing).
- `macos_frontend/api_client.py` (thin httpx wrapper around the
  daemon, currently only used by the mac frontend).
- `macos_frontend/daemon_supervisor.py` (spawns and watches the
  daemon as a child process, handles `EX_CONFIG` stickiness, owns
  the parent-death pipe's write end).
- `scripts/install_launchagent.sh` and the single frontend
  LaunchAgent template.

On macOS the shipped process model is **one LaunchAgent for the
frontend** that supervises the daemon as a child process. The
supervisor respawns on crash, treats `EX_CONFIG` as sticky, and
tears the daemon down via SIGTERM on Quit (the parent-death pipe
catches the cases `atexit` misses). On Linux the supervisor's job
belongs to `systemd --user` with `Restart=on-failure`, so the
Linux frontend does **not** spawn the daemon — it assumes systemd
has it, probes `/health`, and renders `Connecting…` while waiting.

The macOS frontend does not ship a notifier module. An earlier
iteration wrote desktop notifications via rumps + AppleScript; it
was removed along with the associated `NOTIFY_*` env vars in
favour of the always-visible menubar title. The Linux notification
story therefore starts from scratch rather than lifting shared
code.

This means a KDE/Fedora port should keep daemon and most data
semantics unchanged, replacing only frontend/UI and startup
integration.

## 3) Target Linux Architecture

### 3.1 Frontend toolkit choice

Use **PySide6 (Qt for Python)** for KDE-first behavior.

Why:

- Native fit for KDE Plasma.
- Reliable tray support via `QSystemTrayIcon` + `QMenu`.
- Built-in dialogs and drawing primitives for a menu graph replacement.
- Good long-term maintainability for a desktop app.

Plasma-latest requirement:

- Keep the Linux frontend strictly on Qt6 APIs so it tracks Plasma 6 conventions and avoids Qt5 tray edge cases.

### 3.2 Process model

Keep the same 2-process model:

- `daemon` process (unchanged logic, local HTTP API).
- `linux_frontend` tray process (new).

### 3.3 Service/autostart model on Fedora KDE

- Daemon: `systemd --user` service (`Restart=on-failure`).
- Frontend: autostart via `.desktop` file in `~/.config/autostart/` (or user service bound to graphical session target if preferred).

Recommendation for first pass:

- `systemd --user` for daemon.
- `.desktop` autostart for frontend (simpler and robust for GUI startup timing).

Plasma-latest guardrails:

- Add `X-KDE-autostart-after=panel` in the autostart template to reduce race conditions where the tray host is not ready yet.
- Ensure tray availability check at startup (`QSystemTrayIcon.isSystemTrayAvailable()`), with retry/backoff before showing hard failure.

### 3.4 Distribution model (simple dnf install)

Goal UX:

- User runs `sudo dnf install glucobar` after enabling one package source.

Recommended publishing path:

- Build a Fedora RPM (`noarch` expected if pure Python).
- Publish via COPR first (fast iteration), optionally upstream to Fedora package collection later.
- Keep dependencies resolvable by DNF (`python3dist(...)` Requires in spec).

Install command shape (target experience):

- One-time repo enable: `sudo dnf copr enable <owner>/<project>`
- App install: `sudo dnf install glucobar`

## 4) Phased Implementation

## Phase 0 - Packaging and dependency split (low risk)

Problem today:

- `pyproject.toml` has mac-only UI dependency (`rumps`) in default dependencies.
- This makes Linux install paths awkward.

Changes:

- Move frontend deps into extras:
  - `.[macos]` -> `rumps`
  - `.[linux]` -> `PySide6` (and optional Linux notification helper if needed)
- Keep daemon-critical deps in base dependencies.

Acceptance:

- `pip install -e .` is enough for daemon scripts/tests.
- `pip install -e .[linux]` installs Linux tray prerequisites on Fedora.
- `pip install -e .[macos]` preserves current macOS flow.

## Phase 1 - Extract shared frontend logic (medium value, medium effort)

Current duplication risk:

- `macos_frontend/api_client.py` and parts of presentation/threshold logic are mac package-local.
- Linux frontend would otherwise copy behavior and drift.

Create `frontend_common/` for shared, UI-agnostic logic:

- `frontend_common/api_client.py` (move from `macos_frontend/api_client.py`)
- `frontend_common/formatting.py`:
  - status -> title glyph logic (currently `_STATUS_GLYPH` + `_title_parts`)
  - menu status/age text helpers (currently `_humanize_age`, `_refresh_secondary_label`)
- `frontend_common/thresholds.py`:
  - env parsing/validation for low/high/very_high
  - currently `ColorThresholds.from_env()` in `macos_frontend/app.py`
- `frontend_common/notifier_policy.py` **(net-new for the Linux port)**:
  - the previous macOS notifier was removed, so there's no shared
    policy to extract — define the threshold/hysteresis/cooldown state
    machine here first, then build backend adapters per platform.
  - keep backend delivery interface separate.

Refactor macOS frontend to consume shared modules with no behavior change.

Acceptance:

- Existing macOS tests still pass.
- Frontend-common unit tests added for formatting + thresholds + API client parse behavior.

## Phase 2 - Build `linux_frontend` tray app (core delivery)

Add package:

- `linux_frontend/__init__.py`
- `linux_frontend/__main__.py`
- `linux_frontend/app.py`
- `linux_frontend/notifier_backend.py`
- `linux_frontend/graph_widget.py`

Feature parity target with current macOS frontend:

- Tray title/status updates from `GET /glucose/current`.
- Menu actions matching the current macOS dropdown:
  - Refresh (calls `POST /auth/refresh` + immediate tick; shows a
    dim `(Updated now)` / `(N min ago)` freshness hint live-updated
    on menu open)
  - LibreView Account (email + password dialog → Keychain /
    Secret Service via `keyring`)
  - Quit
- Graph preview from `GET /glucose/graph`.
- Same status semantics (`starting`, `ok`, `stale`, `warming_up`, etc.).

Items that are **not** in the current macOS frontend and that the KDE
port should treat as net-new scope rather than parity work:

- Notifications (toggle / threshold alerts) — the macOS notifier was
  removed; bringing them back on Linux means implementing the policy
  module from scratch (see Phase 1).
- Menu-driven threshold editing — thresholds come from
  `THRESHOLD_*_MG_DL` env vars only; do the same on Linux unless a UI
  is explicitly in scope.
- Force Re-Authenticate as a distinct menu item — on macOS this is
  folded into Refresh (which already hits `/auth/refresh`).

KDE-specific implementation notes:

- Use `QSystemTrayIcon` with an icon that updates text/status context in tooltip/menu labels.
- Some KDE setups do not like dynamic text-only tray titles; fallback strategy:
  - primary: concise tooltip + first menu line for value/status
  - optional: generate icon badges for numeric value if needed later
- Keep polling interval behavior consistent with current `FRONTEND_POLL_SECONDS`.
- Prefer symbolic SVG icons that adapt to Plasma light/dark themes.
- Do not rely on legacy StatusNotifier behaviors that are KDE4/Qt5-era only.

Acceptance:

- Linux frontend launches on Fedora KDE and updates every poll tick.
- Menu actions trigger same daemon endpoints as macOS frontend.
- No daemon contract changes required.
- Verified on latest Plasma 6.x Wayland session before release is considered done.

## Phase 3 - Linux notifications and credentials UX

### Notifications

Net-new on Linux — there is no macOS `NotificationPolicy` to port,
since the previous macOS notifier was removed before 0.3. The
Linux implementation has to decide:

- Policy (lives in `frontend_common/notifier_policy.py` per
  Phase 1): threshold bands (`THRESHOLD_*_MG_DL`),
  hysteresis band to prevent edge flapping, per-bucket cooldown,
  and a stale-data alert if the daemon reports `no_network` /
  `stale` past some threshold.
- Delivery backend (`linux_frontend/notifier_backend.py`), ordered
  by preference:
  1. `QSystemTrayIcon.showMessage` if the tray advertises support.
  2. Freedesktop notification over DBus (`org.freedesktop.
     Notifications`) for urgency and action button support.
  3. `notify-send` fallback for minimal installs.

The policy module is the expensive part; the delivery backend is
mostly wiring. Ship the policy with unit tests first, so whichever
delivery path lands can be swapped without re-validating the state
machine.

### Credentials update flow

Use Qt dialogs in frontend:

- Email + password prompt.
- Call `glucose_common.credentials.save_credentials(...)`.

Fedora KDE backend reality:

- `keyring` should map to Secret Service/KWallet when available.
- If unavailable/locked, show an actionable error explaining how to
  unlock KWallet or install a Secret Service provider. There is no
  `.env` fallback for credentials — they're Keychain/keyring-only
  by design, and the error path should make that explicit rather
  than silently reading plain-text secrets.

Acceptance:

- Notification toggle works.
- Alerts fire on the state-machine transitions defined by the new
  `frontend_common/notifier_policy.py` unit tests.
- Credentials save path works on a default Fedora KDE user session.

## Phase 4 - Linux startup/install scripts

Add:

- `scripts/install_systemd.sh` (daemon service install/uninstall/status/logs).
- `scripts/autostart/glucobar-frontend.desktop.tmpl` and installer logic.

Daemon user service unit should include:

- `ExecStart=<venv>/python -m daemon`
- `Restart=on-failure`
- `EnvironmentFile=<repo>/.env` (optional if desired)

Frontend autostart entry should include:

- `Exec=<venv>/python -m linux_frontend`
- `OnlyShowIn=KDE;`
- `X-KDE-autostart-after=panel`

Acceptance:

- Login launches daemon + frontend without manual terminal commands.
- Restart policies behave as expected for daemon crashes.

## Phase 5 - Documentation and QA hardening

Update docs:

- `README.md`:
  - Fedora KDE quick start
  - Linux run commands
  - Linux startup management commands
- Troubleshooting section for:
  - no tray icon
  - notifications disabled by desktop settings
  - keyring backend missing

Add/expand tests:

- Unit tests for frontend-common modules.
- Existing daemon tests remain unchanged and passing.
- Optional smoke test script for Linux frontend startup + daemon reachability.

Acceptance:

- New Linux instructions can be followed cleanly on fresh Fedora KDE setup.
- CI (if configured) runs daemon + shared module tests without Linux GUI dependencies.

## Phase 6 - RPM packaging for `dnf install`

Deliverables:

- `packaging/fedora/glucobar.spec`
- `packaging/fedora/README.md` with build/publish notes
- Optional automation for source tarball + COPR submission

Package content target:

- Python modules (`daemon`, `glucose_common`, `linux_frontend`, shared frontend modules)
- Desktop autostart entry template or installed desktop file
- User service unit/template and helper command docs
- License + README docs in package

Spec design notes:

- Use `%pyproject_*` macros where possible for modern Fedora Python packaging.
- Split optional subpackages if needed:
  - `glucobar` (daemon + shared core)
  - `glucobar-kde` (Linux Plasma frontend/autostart assets), if dependency surface needs separation.
- Ensure scriptlets do **not** force-start user services during RPM transaction; provide post-install guidance instead.

Acceptance:

- Fresh Fedora KDE host can install with:
  - `sudo dnf copr enable <owner>/<project>`
  - `sudo dnf install glucobar`
- `dnf upgrade` updates to newer app versions cleanly.
- Uninstall removes package-owned files without leaving broken user-level config.

## 5) Proposed File-Level Worklist

## New files

- `kde-fedora-implementation-plan.md` (this file)
- `frontend_common/api_client.py`
- `frontend_common/formatting.py`
- `frontend_common/thresholds.py`
- `frontend_common/notifier_policy.py`
- `linux_frontend/__init__.py`
- `linux_frontend/__main__.py`
- `linux_frontend/app.py`
- `linux_frontend/notifier_backend.py`
- `linux_frontend/graph_widget.py`
- `scripts/install_systemd.sh`
- `scripts/autostart/glucobar-frontend.desktop.tmpl`
- `packaging/fedora/glucobar.spec`
- `packaging/fedora/README.md`

## Modified files

- `pyproject.toml` (dependency split and packages list)
- `macos_frontend/app.py` (imports from shared modules)
- `README.md` (Linux section)
- tests under `tests/` for shared logic

(`macos_frontend/notifier.py` no longer exists — don't plan an edit
there. Any notifier work happens in the new `frontend_common/` module
and the per-platform backends.)

## 6) Risks and Mitigations

- Tray rendering differences in Plasma themes/Wayland:
  - Mitigation: rely on tooltip + menu first line as canonical status text.
- Notification delivery inconsistencies:
  - Mitigation: layered backend fallback (`showMessage` -> DBus -> `notify-send`).
- Keyring backend differences between environments:
  - Mitigation: detect absence/locked state at startup and surface a
    clear remediation message rather than falling back to plain-text
    env vars. Document `gnome-keyring --unlock` / `kwalletmanager5`
    steps in the troubleshooting README.
- Dependency bloat from Qt:
  - Mitigation: keep Qt in Linux extra, not base install.

## 7) Validation Matrix (Fedora KDE, Plasma Latest)

Minimum manual validation:

- Fedora KDE latest two releases (e.g., current stable and previous stable), each on latest available Plasma 6.x.
- Session coverage:
  - Wayland (required pass)
  - X11 (best-effort compatibility pass)
- Fresh boot/login:
  - daemon started
  - frontend visible
  - updates arrive
- Error path checks:
  - daemon stopped -> frontend shows offline state
  - bad credentials -> `auth_failed` handling
  - simulated stale values -> stale UI + notification behavior
- Graph checks:
  - data appears after successful polls
  - empty state shown cleanly when history unavailable
- Theme checks:
  - Breeze light and dark theme
  - tray icon and graph remain legible in both themes
- Desktop shell checks:
  - panel restart (`plasmashell --replace`) does not require app restart for tray recovery

## 8) Suggested Delivery Order (Practical)

1. Phase 0 dependency split.
2. Phase 1 shared module extraction (keep mac behavior unchanged).
3. Phase 2 Linux tray MVP without graph (status + actions first).
4. Add graph widget.
5. Add install scripts/autostart.
6. Docs and QA pass.
7. Add RPM packaging + COPR publishing flow.

This order reduces risk: get a functioning KDE tray quickly, then layer parity features.

## 9) Effort Estimate

Assuming familiarity with Qt/PySide and no major environment surprises:

- Phase 0-1: 0.5-1.5 days
- Phase 2 (tray + actions): 1-2 days
- Graph + notifications + credential UX: 1-2 days
- Install/docs/QA: 0.5-1 day
- RPM/COPR packaging and validation: 0.5-1.5 days

Total: roughly **4-7 focused days** for a polished Fedora KDE-first implementation with `dnf` installability.

## 10) Definition of Done for Plasma Latest

The Linux port is considered complete for KDE support only when all are true:

- Passes validation on Fedora KDE latest two stable releases.
- Passes on latest Plasma 6.x Wayland session (mandatory).
- No Qt5-only dependencies in Linux path.
- Tray icon, notifications, credentials dialog, thresholds dialog, and graph are all functional after login autostart.
- README documents the supported Plasma/Fedora window and known limitations.
- Documented and working `dnf` install path (`copr enable` + `dnf install`) for latest Fedora KDE releases.

