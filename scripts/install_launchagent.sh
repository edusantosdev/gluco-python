#!/usr/bin/env bash
# Install / uninstall the LaunchAgents for glucose-display.
#
# Usage:
#   scripts/install_launchagent.sh install     # render templates + bootstrap
#   scripts/install_launchagent.sh uninstall   # bootout + remove plists
#   scripts/install_launchagent.sh status      # is launchd running them?
#   scripts/install_launchagent.sh logs        # tail launchd stdout/stderr
#
# The templates under scripts/launchagents/ use __VENV_PYTHON__,
# __REPO_ROOT__, __STATE_DIR__, and __APP_EXECUTABLE__ placeholders so
# the installed plists are fully qualified paths — launchd runs without
# a useful $PATH, and plists with bare commands just silently fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
STATE_DIR="${DAEMON_STATE_DIR:-$HOME/.glucose-display}"

DAEMON_LABEL="com.glucose-display.daemon"
FRONTEND_LABEL="com.glucose-display.frontend"

LA_DIR="${HOME}/Library/LaunchAgents"
DAEMON_PLIST="${LA_DIR}/${DAEMON_LABEL}.plist"
FRONTEND_PLIST="${LA_DIR}/${FRONTEND_LABEL}.plist"

DAEMON_TMPL="${REPO_ROOT}/scripts/launchagents/${DAEMON_LABEL}.plist.tmpl"
FRONTEND_TMPL="${REPO_ROOT}/scripts/launchagents/${FRONTEND_LABEL}.plist.tmpl"

# The frontend runs out of a thin .app bundle so AppKit sees a proper
# CFBundleIdentifier / LSUIElement. See scripts/app_bundle/Info.plist.tmpl
# for the full explanation.
APP_BUNDLE_SRC="${REPO_ROOT}/scripts/app_bundle"
APP_BUNDLE_DST="${REPO_ROOT}/build/Glucose Menubar.app"
# ``APP_EXECUTABLE`` is the symlinked venv Python inside the bundle.
# launchd execs it with ``-m macos_frontend``; Launch Services keys the
# bundle to that executable path, which is what gives AppKit our
# ``CFBundleIdentifier``.
APP_EXECUTABLE="${APP_BUNDLE_DST}/Contents/MacOS/Glucose Menubar"

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
    if launchctl bootstrap "${GUI_DOMAIN}" "${plist}"; then
        return 0
    fi
    echo "warn: bootstrap failed for ${label}; retrying after forced bootout" >&2
    bootout_agent "${label}" "${plist}"
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
    #   Glucose Menubar.app/
    #     Contents/
    #       Info.plist
    #       pyvenv.cfg                       (copy of .venv/pyvenv.cfg)
    #       lib/python3.x/site-packages/     (symlink → .venv/...)
    #       MacOS/
    #         Glucose Menubar                (symlink → .venv/bin/python)
    local contents="${APP_BUNDLE_DST}/Contents"
    local macos="${contents}/MacOS"
    mkdir -p "${macos}" "${contents}/Resources"

    sed \
        -e "s|__VENV_PYTHON__|${VENV_PYTHON}|g" \
        -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
        "${APP_BUNDLE_SRC}/Info.plist.tmpl" > "${contents}/Info.plist"
    plutil -lint "${contents}/Info.plist" >/dev/null

    # ``CFBundleExecutable`` in Info.plist is ``Glucose Menubar``; this
    # symlink is what launchd actually execs, and what Python sees as
    # its own executable path.
    ln -sfn "${VENV_PYTHON}" "${macos}/Glucose Menubar"

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

cmd_install() {
    if [ ! -x "${VENV_PYTHON}" ]; then
        echo "error: ${VENV_PYTHON} not found. Create the venv first:" >&2
        echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
        exit 1
    fi
    echo >&2 "Note: stop any manually started daemon first (terminal:"
    echo >&2 "  .venv/bin/python -m daemon"
    echo >&2 "Only one process may hold ${STATE_DIR}/daemon.lock — otherwise"
    echo >&2 "this LaunchAgent will fail with 'Another glucose daemon is already running'."
    mkdir -p "${LA_DIR}" "${STATE_DIR}"

    # The frontend plist's ProgramArguments[0] points at the symlinked
    # Python *inside* the bundle, so the bundle has to exist before we
    # bootstrap.
    build_app_bundle

    render_plist "${DAEMON_TMPL}" "${DAEMON_PLIST}"
    render_plist "${FRONTEND_TMPL}" "${FRONTEND_PLIST}"
    # Plists under ~/Library/LaunchAgents must not be world-writable.
    chmod 0644 "${DAEMON_PLIST}" "${FRONTEND_PLIST}"

    # "bootstrap" is the modern replacement for "load". It's a no-op if
    # the label is already loaded, so we unload first to pick up template
    # changes (path moves, env var additions, etc.).
    bootout_agent "${DAEMON_LABEL}" "${DAEMON_PLIST}"
    bootout_agent "${FRONTEND_LABEL}" "${FRONTEND_PLIST}"
    bootstrap_agent "${DAEMON_LABEL}" "${DAEMON_PLIST}"
    bootstrap_agent "${FRONTEND_LABEL}" "${FRONTEND_PLIST}"
    # kickstart triggers an immediate start without waiting for login.
    launchctl kickstart -k "${GUI_DOMAIN}/${DAEMON_LABEL}"   || true
    launchctl kickstart -k "${GUI_DOMAIN}/${FRONTEND_LABEL}" || true

    echo "Installed LaunchAgents:"
    echo "  ${DAEMON_PLIST}"
    echo "  ${FRONTEND_PLIST}"
    echo
    echo "Check status:  scripts/install_launchagent.sh status"
    echo "Tail logs:     scripts/install_launchagent.sh logs"
}

cmd_uninstall() {
    bootout_agent "${DAEMON_LABEL}" "${DAEMON_PLIST}"
    bootout_agent "${FRONTEND_LABEL}" "${FRONTEND_PLIST}"
    rm -f "${DAEMON_PLIST}" "${FRONTEND_PLIST}"
    echo "Removed LaunchAgents (plist files and loaded jobs)."
}

cmd_status() {
    # launchctl print returns 113 when the label isn't loaded. Show a
    # friendly summary rather than the 300-line raw dump.
    for label in "${DAEMON_LABEL}" "${FRONTEND_LABEL}"; do
        if launchctl print "${GUI_DOMAIN}/${label}" >/dev/null 2>&1; then
            local state
            state=$(launchctl print "${GUI_DOMAIN}/${label}" | awk '/state = /{print $3; exit}')
            echo "${label}: ${state:-unknown}"
        else
            echo "${label}: not loaded"
        fi
    done
}

cmd_logs() {
    echo "=== ${STATE_DIR}/launchd.daemon.{out,err}.log ==="
    tail -n 40 "${STATE_DIR}/launchd.daemon.out.log" \
              "${STATE_DIR}/launchd.daemon.err.log" 2>/dev/null || true
    echo
    echo "=== ${STATE_DIR}/launchd.frontend.{out,err}.log ==="
    tail -n 40 "${STATE_DIR}/launchd.frontend.out.log" \
              "${STATE_DIR}/launchd.frontend.err.log" 2>/dev/null || true
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
