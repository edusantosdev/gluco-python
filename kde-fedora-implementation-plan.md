# KDE Plasma (Fedora) Implementation Plan

This plan is based on the current codebase state (daemon + `macos_frontend`, including graph support via `/glucose/graph` and `macos_frontend/graph_view.py`).

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

## 2) Current Architecture Review (Updated)

The current layout is already favorable for a Linux UI port:

- `daemon/` is portable Python FastAPI + background poller.
- `daemon/main.py` exposes stable endpoints:
  - `GET /glucose/current`
  - `GET /glucose/graph?hours=...`
  - `POST /auth/refresh`
  - `GET /health`, `GET /patient`
- `daemon/service.py` already handles auth refresh, backoff, warm-up detection, stale detection, and graph history caching.
- `glucose_common/credentials.py` uses `keyring` with env fallback (portable conceptually, backend differs by OS).

macOS-specific pieces are concentrated in:

- `macos_frontend/app.py` (rumps/AppKit/NSAlert/AppleScript paths).
- `macos_frontend/graph_view.py` (AppKit drawing).
- `macos_frontend/notifier.py` (rumps + AppleScript notification delivery).
- `scripts/install_launchagent.sh` and launch agent templates.

This means a KDE/Fedora port should keep daemon and most data semantics unchanged, replacing only frontend/UI and startup integration.

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

- User runs `sudo dnf install glucose-display` after enabling one package source.

Recommended publishing path:

- Build a Fedora RPM (`noarch` expected if pure Python).
- Publish via COPR first (fast iteration), optionally upstream to Fedora package collection later.
- Keep dependencies resolvable by DNF (`python3dist(...)` Requires in spec).

Install command shape (target experience):

- One-time repo enable: `sudo dnf copr enable <owner>/<project>`
- App install: `sudo dnf install glucose-display`

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
  - status -> title glyph logic
  - menu status/age text helpers
- `frontend_common/thresholds.py`:
  - env parsing/validation for low/high/very_high
- `frontend_common/notifier_policy.py`:
  - move policy/state machine from current `macos_frontend/notifier.py`
  - keep backend delivery interface separate

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
- Menu actions:
  - Refresh Now
  - Force Re-Authenticate
  - Notifications (toggle)
  - Update Credentials...
  - Set Thresholds...
  - Quit
- Graph preview from `GET /glucose/graph`.
- Same status semantics (`starting`, `ok`, `stale`, `warming_up`, etc.).

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

Implement Linux backend for the shared notification policy:

- Keep current `NotificationPolicy` semantics (hysteresis/cooldown/stale alerts).
- Delivery options (ordered):
  1. `QSystemTrayIcon.showMessage` if available.
  2. Freedesktop notification over DBus (optional enhancement).
  3. `notify-send` fallback (if installed).

### Credentials update flow

Use Qt dialogs in frontend:

- Email + password prompt.
- Call `glucose_common.credentials.save_credentials(...)`.

Fedora KDE backend reality:

- `keyring` should map to Secret Service/KWallet when available.
- If unavailable/locked, show actionable error and remind `.env` fallback.

Acceptance:

- Notification toggle works.
- Alerts fire on same policy transitions as current tests define.
- Credentials save path works on a default Fedora KDE user session.

## Phase 4 - Linux startup/install scripts

Add:

- `scripts/install_systemd.sh` (daemon service install/uninstall/status/logs).
- `scripts/autostart/glucose-display-frontend.desktop.tmpl` and installer logic.

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

- `packaging/fedora/glucose-display.spec`
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
  - `glucose-display` (daemon + shared core)
  - `glucose-display-kde` (Linux Plasma frontend/autostart assets), if dependency surface needs separation.
- Ensure scriptlets do **not** force-start user services during RPM transaction; provide post-install guidance instead.

Acceptance:

- Fresh Fedora KDE host can install with:
  - `sudo dnf copr enable <owner>/<project>`
  - `sudo dnf install glucose-display`
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
- `scripts/autostart/glucose-display-frontend.desktop.tmpl`
- `packaging/fedora/glucose-display.spec`
- `packaging/fedora/README.md`

## Modified files

- `pyproject.toml` (dependency split and packages list)
- `macos_frontend/app.py` (imports from shared modules)
- `macos_frontend/notifier.py` (or split into policy + mac backend adapter)
- `README.md` (Linux section)
- tests under `tests/` for shared logic

## 6) Risks and Mitigations

- Tray rendering differences in Plasma themes/Wayland:
  - Mitigation: rely on tooltip + menu first line as canonical status text.
- Notification delivery inconsistencies:
  - Mitigation: layered backend fallback (`showMessage` -> DBus -> `notify-send`).
- Keyring backend differences between environments:
  - Mitigation: keep `.env` fallback and improve user-facing errors.
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

