#!/usr/bin/env bash
# Build a standalone GlucoBar.app with py2app.
#
# Usage:
#   scripts/build_portable_app.sh
#
# Output:
#   dist/GlucoBar.app

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${REPO_ROOT}/.venv/bin/python"
TMP_SETUP_DIR=""

cleanup() {
    if [ -n "${TMP_SETUP_DIR}" ] && [ -d "${TMP_SETUP_DIR}" ]; then
        rm -rf "${TMP_SETUP_DIR}"
    fi
}
trap cleanup EXIT

if [ ! -x "${PYTHON}" ]; then
    echo "error: ${PYTHON} not found. Create the venv first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev,build]'" >&2
    exit 1
fi

echo "Installing build dependencies (py2app)..."
"${PYTHON}" -m pip install -e "${REPO_ROOT}[build]" >/dev/null

ICON_ICNS="${REPO_ROOT}/scripts/app_bundle/GlucoBar.icns"
if [ ! -f "${ICON_ICNS}" ]; then
    echo "Generating ${ICON_ICNS}..."
    "${PYTHON}" "${REPO_ROOT}/scripts/build_glucobar_icns.py"
fi

echo "Building standalone app (py2app)..."
# Stage the bundle under ``build/`` so we never delete all of ``dist/``
# (release zips and other artifacts often sit there). Only replace
# ``dist/GlucoBar.app``. Use ``/bin/rm`` so a user ``rm`` alias cannot
# break cleanup.
/bin/rm -rf "${REPO_ROOT}/build/py2app"
STAGING_APP_OUT="${REPO_ROOT}/build/py2app_app_out"
/bin/rm -rf "${STAGING_APP_OUT}"
mkdir -p "${STAGING_APP_OUT}"

# py2app currently does not play well with PEP 621 metadata that maps
# dependencies to ``install_requires`` in the active project's
# ``pyproject.toml``. Run py2app from an isolated temporary setup.py so
# setuptools does not load this repo's project metadata.
TMP_SETUP_DIR="$(mktemp -d)"
cat > "${TMP_SETUP_DIR}/setup.py" <<EOF
from setuptools import setup

APP = ["${REPO_ROOT}/macos_frontend/__main__.py"]

OPTIONS = {
    "argv_emulation": False,
    "strip": False,
    "optimize": 1,
    "iconfile": "${ICON_ICNS}",
    "packages": [
        "daemon",
        "glucose_common",
        "macos_frontend",
        "rumps",
        "httpx",
        "fastapi",
        "uvicorn",
        "pydantic_settings",
        "dotenv",
        "keyring",
        "pylibrelinkup",
    ],
    # Test-only (dev extra): reached via fastapi.testclient -> starlette.testclient,
    # which the app never imports. The daemon client uses httpx.
    "excludes": ["httpx2", "httpcore2", "truststore"],
    "plist": {
        "CFBundleName": "GlucoBar",
        "CFBundleDisplayName": "GlucoBar",
        "CFBundleIdentifier": "com.glucobar.frontend",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "CFBundleIconFile": "GlucoBar",
        "NSHumanReadableCopyright": (
            "Copyright © 2026 Eduardo Santos. Apache-2.0. Provided as-is; not a medical device."
        ),
    },
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
)
EOF

(
    cd "${TMP_SETUP_DIR}"
    PYTHONPATH="${REPO_ROOT}" "${PYTHON}" setup.py py2app \
        --dist-dir "${STAGING_APP_OUT}" \
        --bdist-base "${REPO_ROOT}/build/py2app"
)

STAGED_BUNDLE="${STAGING_APP_OUT}/GlucoBar.app"
if [ ! -d "${STAGED_BUNDLE}" ]; then
    echo "error: py2app did not produce ${STAGED_BUNDLE}" >&2
    exit 1
fi
mkdir -p "${REPO_ROOT}/dist"
if [ -e "${REPO_ROOT}/dist/GlucoBar.app" ]; then
    chmod -R u+w "${REPO_ROOT}/dist/GlucoBar.app" 2>/dev/null || true
    /bin/rm -rf "${REPO_ROOT}/dist/GlucoBar.app"
fi
mv "${STAGED_BUNDLE}" "${REPO_ROOT}/dist/"
/bin/rm -rf "${STAGING_APP_OUT}"

APP_BUNDLE="${REPO_ROOT}/dist/GlucoBar.app"
MACOS_DIR="${APP_BUNDLE}/Contents/MacOS"

# Rename the subprocess Python launcher inside the bundle so macOS
# permission dialogs (Keychain, network, etc.) triggered by any
# subprocess show a recognisable "GlucoBarDaemon" identity instead of
# the generic "python". py2app keeps the original ``python`` binary in
# place; we add a sibling copy with a meaningful name so the supervisor
# can launch the daemon under that identity.
if [ -x "${MACOS_DIR}/python" ] && [ ! -e "${MACOS_DIR}/GlucoBarDaemon" ]; then
    cp -p "${MACOS_DIR}/python" "${MACOS_DIR}/GlucoBarDaemon"
    chmod 0755 "${MACOS_DIR}/GlucoBarDaemon"
fi

# Ad-hoc codesign the bundle so macOS treats it as a stable identity.
# This reduces Keychain "allow?" re-prompts across future launches of
# the same binary. Requires Xcode Command Line Tools (``codesign``).
if command -v codesign >/dev/null 2>&1; then
    echo "Ad-hoc signing ${APP_BUNDLE}..."
    codesign --force --deep --sign - "${APP_BUNDLE}" || \
        echo "warn: ad-hoc codesign failed; continuing (the .app will still run)." >&2
else
    echo "warn: codesign not found; skipping ad-hoc signature." >&2
fi

echo
echo "Build complete:"
echo "  ${APP_BUNDLE}"
echo
echo "Quick smoke run:"
echo "  open \"${APP_BUNDLE}\""
