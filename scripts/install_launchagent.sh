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
# __REPO_ROOT__, and __STATE_DIR__ placeholders so the installed plists
# are fully qualified paths — launchd runs without a useful $PATH, and
# plists with bare commands just silently fail.

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

GUI_DOMAIN="gui/$(id -u)"

render_plist() {
    local tmpl="$1" dest="$2"
    sed \
        -e "s|__VENV_PYTHON__|${VENV_PYTHON}|g" \
        -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
        -e "s|__STATE_DIR__|${STATE_DIR}|g" \
        "${tmpl}" > "${dest}"
    # plutil verifies well-formedness; bail early if we produced bad XML.
    plutil -lint "${dest}" >/dev/null
}

cmd_install() {
    if [ ! -x "${VENV_PYTHON}" ]; then
        echo "error: ${VENV_PYTHON} not found. Create the venv first:" >&2
        echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
        exit 1
    fi
    mkdir -p "${LA_DIR}" "${STATE_DIR}"

    render_plist "${DAEMON_TMPL}" "${DAEMON_PLIST}"
    render_plist "${FRONTEND_TMPL}" "${FRONTEND_PLIST}"
    # Plists under ~/Library/LaunchAgents must not be world-writable.
    chmod 0644 "${DAEMON_PLIST}" "${FRONTEND_PLIST}"

    # "bootstrap" is the modern replacement for "load". It's a no-op if
    # the label is already loaded, so we unload first to pick up template
    # changes (path moves, env var additions, etc.).
    launchctl bootout "${GUI_DOMAIN}/${DAEMON_LABEL}"   2>/dev/null || true
    launchctl bootout "${GUI_DOMAIN}/${FRONTEND_LABEL}" 2>/dev/null || true
    launchctl bootstrap "${GUI_DOMAIN}" "${DAEMON_PLIST}"
    launchctl bootstrap "${GUI_DOMAIN}" "${FRONTEND_PLIST}"
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
    launchctl bootout "${GUI_DOMAIN}/${DAEMON_LABEL}"   2>/dev/null || true
    launchctl bootout "${GUI_DOMAIN}/${FRONTEND_LABEL}" 2>/dev/null || true
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
