#!/usr/bin/env bash
# Install / uninstall the Gluco LaunchAgent.
#
# Usage:
#   scripts/install_launchagent.sh install     # render template + bootstrap
#   scripts/install_launchagent.sh uninstall   # bootout + remove plist
#   scripts/install_launchagent.sh status      # is launchd running it?
#   scripts/install_launchagent.sh logs        # tail stdout/stderr
#
# Gluco is strictly single-wrapper / single-LaunchAgent: the
# frontend is the only thing ``launchctl bootstrap``ed, and the daemon
# always runs as a child process of the frontend under
# ``macos_frontend.daemon_supervisor``. There is intentionally no
# standalone daemon LaunchAgent. Legacy labels (``com.glucobar.daemon``
# and the pre-rename ``com.glucose-display.*`` pair) are only kept as
# sweep targets so upgrades can bootout + delete any leftovers from
# older installs.
#
# The templates under scripts/launchagents/ use __VENV_PYTHON__,
# __REPO_ROOT__, __STATE_DIR__, and __APP_EXECUTABLE__ placeholders so
# the installed plists are fully qualified paths — launchd runs without
# a useful $PATH, and plists with bare commands just silently fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
STATE_DIR="${DAEMON_STATE_DIR:-$HOME/.glucobar}"

FRONTEND_LABEL="com.glucobar.frontend"

# Labels we *used* to install. Every ``install`` sweeps these out so
# upgrades don't leave a second daemon racing the supervised child
# for the same LibreView session. Both pre-rename (pre-0.3,
# ``glucose-display``) and post-rename-pre-supervisor (0.3.0
# two-LaunchAgent model) labels are in this list.
LEGACY_DAEMON_LABEL="com.glucobar.daemon"
LEGACY_DAEMON_DISPLAY_LABEL="com.glucose-display.daemon"
LEGACY_FRONTEND_DISPLAY_LABEL="com.glucose-display.frontend"

LA_DIR="${HOME}/Library/LaunchAgents"
FRONTEND_PLIST="${LA_DIR}/${FRONTEND_LABEL}.plist"
LEGACY_DAEMON_PLIST="${LA_DIR}/${LEGACY_DAEMON_LABEL}.plist"
LEGACY_DAEMON_DISPLAY_PLIST="${LA_DIR}/${LEGACY_DAEMON_DISPLAY_LABEL}.plist"
LEGACY_FRONTEND_DISPLAY_PLIST="${LA_DIR}/${LEGACY_FRONTEND_DISPLAY_LABEL}.plist"

FRONTEND_TMPL="${REPO_ROOT}/scripts/launchagents/${FRONTEND_LABEL}.plist.tmpl"

# The frontend runs out of a thin .app bundle so AppKit sees a proper
# CFBundleIdentifier / LSUIElement. See scripts/app_bundle/Info.plist.tmpl
# for the full explanation.
APP_BUNDLE_SRC="${REPO_ROOT}/scripts/app_bundle"
APP_BUNDLE_DST="${REPO_ROOT}/build/Gluco.app"
LEGACY_APP_BUNDLE_DST="${REPO_ROOT}/build/Glucose Menubar.app"
# ``APP_EXECUTABLE`` is a real Python binary inside the bundle.
# launchd execs it with ``-m macos_frontend``; keeping argv[0] inside the
# .app is what gives AppKit our ``CFBundleIdentifier``.
APP_EXECUTABLE="${APP_BUNDLE_DST}/Contents/MacOS/Gluco"

GUI_DOMAIN="gui/$(id -u)"

bootout_agent() {
    local label="$1" plist="$2"
    # Remove by both label and plist path. Depending on launchd's current
    # bookkeeping, one form may succeed while the other reports "not found".
    launchctl bootout "${GUI_DOMAIN}/${label}" 2>/dev/null || true
    launchctl bootout "${GUI_DOMAIN}" "${plist}" 2>/dev/null || true
}

bootstrap_agent() {
    local label="$1" plist="$2"
    # launchd remembers per-label "disabled" state independently of
    # whether the label is currently loaded. A previous ``bootout``
    # or an explicit ``launchctl disable`` will make ``bootstrap``
    # fail with the uninformative ``5: Input/output error`` until we
    # re-enable the label. This is a no-op if the label was already
    # enabled.
    launchctl enable "${GUI_DOMAIN}/${label}" 2>/dev/null || true
    if launchctl bootstrap "${GUI_DOMAIN}" "${plist}"; then
        return 0
    fi
    echo "warn: bootstrap failed for ${label}; retrying after forced bootout" >&2
    bootout_agent "${label}" "${plist}"
    launchctl enable "${GUI_DOMAIN}/${label}" 2>/dev/null || true
    launchctl bootstrap "${GUI_DOMAIN}" "${plist}"
}

render_plist() {
    local tmpl="$1" dest="$2"
    sed \
        -e "s|__VENV_PYTHON__|${VENV_PYTHON}|g" \
        -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
        -e "s|__STATE_DIR__|${STATE_DIR}|g" \
        -e "s|__APP_EXECUTABLE__|${APP_EXECUTABLE}|g" \
        "${tmpl}" > "${dest}"
    # plutil verifies well-formedness; bail early if we produced bad XML.
    plutil -lint "${dest}" >/dev/null
}

build_app_bundle() {
    # The bundle is a self-contained "virtual venv" around the real
    # .venv so that when the Python interpreter launches from inside
    # ``Contents/MacOS/``, both of the following are true:
    #
    #   1. ``NSBundle.mainBundle()`` resolves to the .app, by walking
    #      up from the executable path and finding ``Contents/Info.plist``.
    #      This is what gives AppKit our ``CFBundleIdentifier`` and
    #      ``LSUIElement`` (fixes the dialog-crash loop).
    #
    #   2. Python still picks up the real venv's ``site-packages`` (so
    #      ``rumps``, ``httpx``, ``pyobjc`` etc. are importable). CPython
    #      looks for ``pyvenv.cfg`` one directory above the executable,
    #      then uses ``sys.prefix = <that dir>`` for site-packages
    #      discovery. So we place ``pyvenv.cfg`` at ``Contents/`` and
    #      symlink ``Contents/lib/python*/site-packages`` into the real
    #      venv.
    #
    # Layout produced here:
    #
    #   Gluco.app/
    #     Contents/
    #       Info.plist
    #       pyvenv.cfg                       (copy of .venv/pyvenv.cfg)
    #       lib/python3.x/site-packages/     (symlink → .venv/...)
    #       MacOS/
    #         Gluco                       (copied Python launcher)
    local contents="${APP_BUNDLE_DST}/Contents"
    local macos="${contents}/MacOS"
    mkdir -p "${macos}" "${contents}/Resources"

    sed \
        -e "s|__VENV_PYTHON__|${VENV_PYTHON}|g" \
        -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
        "${APP_BUNDLE_SRC}/Info.plist.tmpl" > "${contents}/Info.plist"
    plutil -lint "${contents}/Info.plist" >/dev/null

    if [ -f "${APP_BUNDLE_SRC}/Gluco.icns" ]; then
        cp -f "${APP_BUNDLE_SRC}/Gluco.icns" "${contents}/Resources/"
    else
        echo "warn: ${APP_BUNDLE_SRC}/Gluco.icns missing; run:" >&2
        echo "  .venv/bin/python scripts/build_gluco_icns.py" >&2
    fi

    # Liquid Glass icon for macOS 26+ (see ``scripts/compile_app_icon.sh``).
    rm -f "${contents}/Resources/Assets.car"
    if "${REPO_ROOT}/scripts/compile_app_icon.sh" "${contents}/Resources"; then
        plutil -replace CFBundleIconName -string Gluco "${contents}/Info.plist"
    fi

    # ``CFBundleExecutable`` in Info.plist is ``Gluco``; keep a real
    # executable file in ``Contents/MacOS`` so process path remains inside
    # the bundle (symlinks can resolve to .venv/bin/python and lose bundle
    # identity under launchd).
    rm -f "${macos}/Gluco"
    cp -f "${VENV_PYTHON}" "${macos}/Gluco"
    chmod 0755 "${macos}/Gluco"

    # Copy pyvenv.cfg so Python's venv site-packages discovery finds
    # it when launched from ``Contents/MacOS/``. We copy instead of
    # symlinking because the file is tiny and it keeps the bundle
    # self-describing.
    cp "${REPO_ROOT}/.venv/pyvenv.cfg" "${contents}/pyvenv.cfg"

    # Symlink site-packages so all installed deps are visible.
    local py_version
    py_version=$("${VENV_PYTHON}" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')
    local sp_parent="${contents}/lib/${py_version}"
    mkdir -p "${sp_parent}"
    ln -sfn "${REPO_ROOT}/.venv/lib/${py_version}/site-packages" "${sp_parent}/site-packages"

    # Register the new bundle with Launch Services so macOS knows
    # about the CFBundleIdentifier / LSUIElement. Without this, the
    # first launch can still be treated as unbundled.
    /System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister \
        -f "${APP_BUNDLE_DST}" >/dev/null 2>&1 || true
}

sweep_legacy_daemon_agents() {
    # Every ``install`` flushes any previously-installed daemon
    # LaunchAgent (from 0.3.0's two-LaunchAgent model, or from the
    # pre-rename "glucose-display" era). Two daemons against the same
    # account will both authenticate and both poll, which reliably
    # trips Abbott's rate limiter.
    bootout_agent "${LEGACY_DAEMON_LABEL}" "${LEGACY_DAEMON_PLIST}"
    bootout_agent "${LEGACY_DAEMON_DISPLAY_LABEL}" "${LEGACY_DAEMON_DISPLAY_PLIST}"
    bootout_agent "${LEGACY_FRONTEND_DISPLAY_LABEL}" "${LEGACY_FRONTEND_DISPLAY_PLIST}"
    rm -f "${LEGACY_DAEMON_PLIST}" \
          "${LEGACY_DAEMON_DISPLAY_PLIST}" \
          "${LEGACY_FRONTEND_DISPLAY_PLIST}"
    rm -rf "${LEGACY_APP_BUNDLE_DST}"
}

cmd_install() {
    if [ ! -x "${VENV_PYTHON}" ]; then
        echo "error: ${VENV_PYTHON} not found. Create the venv first:" >&2
        echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
        exit 1
    fi
    echo >&2 "Note: stop any manually started daemon first (terminal:"
    echo >&2 "  .venv/bin/python -m daemon"
    echo >&2 "Only one process may hold ${STATE_DIR}/daemon.lock — the"
    echo >&2 "supervised daemon will refuse to start otherwise."
    mkdir -p "${LA_DIR}" "${STATE_DIR}"

    # The frontend plist's ProgramArguments[0] points at the symlinked
    # Python *inside* the bundle, so the bundle has to exist before we
    # bootstrap.
    build_app_bundle

    render_plist "${FRONTEND_TMPL}" "${FRONTEND_PLIST}"
    # Plists under ~/Library/LaunchAgents must not be world-writable.
    chmod 0644 "${FRONTEND_PLIST}"

    sweep_legacy_daemon_agents

    # "bootstrap" is the modern replacement for "load". It's a no-op if
    # the label is already loaded, so we unload first to pick up template
    # changes (path moves, env var additions, etc.).
    bootout_agent "${FRONTEND_LABEL}" "${FRONTEND_PLIST}"
    bootstrap_agent "${FRONTEND_LABEL}" "${FRONTEND_PLIST}"
    # kickstart triggers an immediate start without waiting for login.
    launchctl kickstart -k "${GUI_DOMAIN}/${FRONTEND_LABEL}" || true

    echo "Installed LaunchAgent:"
    echo "  ${FRONTEND_PLIST}"
    echo "  (the frontend supervises the daemon as a child process)"
    echo
    echo "Check status:  scripts/install_launchagent.sh status"
    echo "Tail logs:     scripts/install_launchagent.sh logs"
}

cmd_uninstall() {
    bootout_agent "${FRONTEND_LABEL}" "${FRONTEND_PLIST}"
    sweep_legacy_daemon_agents
    rm -f "${FRONTEND_PLIST}"
    echo "Removed LaunchAgent (plist file and loaded job)."
}

cmd_status() {
    # launchctl print returns 113 when the label isn't loaded. Show a
    # friendly summary rather than the 300-line raw dump.
    if launchctl print "${GUI_DOMAIN}/${FRONTEND_LABEL}" >/dev/null 2>&1; then
        local state
        state=$(launchctl print "${GUI_DOMAIN}/${FRONTEND_LABEL}" \
                | awk '/state = /{print $3; exit}')
        echo "${FRONTEND_LABEL}: ${state:-unknown}"
    else
        echo "${FRONTEND_LABEL}: not loaded"
    fi
}

cmd_logs() {
    # The frontend's launchd redirected stdio, plus the daemon child's
    # stdio captured by the supervisor, plus the daemon's own rotating
    # application log. Between them these three cover every crash
    # and restart.
    echo "=== ${STATE_DIR}/launchd.frontend.{out,err}.log ==="
    tail -n 40 "${STATE_DIR}/launchd.frontend.out.log" \
              "${STATE_DIR}/launchd.frontend.err.log" 2>/dev/null || true
    echo
    echo "=== ${STATE_DIR}/daemon.{stdout,stderr}.log  (supervisor) ==="
    tail -n 40 "${STATE_DIR}/daemon.stdout.log" \
              "${STATE_DIR}/daemon.stderr.log" 2>/dev/null || true
    echo
    echo "=== ${STATE_DIR}/logs/daemon.log  (daemon rotating log) ==="
    tail -n 40 "${STATE_DIR}/logs/daemon.log" 2>/dev/null || true
}

case "${1:-}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    *)
        echo "usage: $0 {install|uninstall|status|logs}" >&2
        exit 2
        ;;
esac
