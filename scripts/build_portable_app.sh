#!/usr/bin/env bash
# Build a standalone Glucobar.app with py2app.
#
# Usage:
#   scripts/build_portable_app.sh
#
# Output:
#   dist/Glucobar.app

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
    echo "  python3 -m venv .venv && .venv/bin/python -m pip install -e .[dev,build]" >&2
    exit 1
fi

echo "Installing build dependencies (py2app)..."
"${PYTHON}" -m pip install -e "${REPO_ROOT}[build]" >/dev/null

echo "Building standalone app (py2app)..."
rm -rf build dist

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
    "plist": {
        "CFBundleName": "Glucobar",
        "CFBundleDisplayName": "Glucobar",
        "CFBundleIdentifier": "com.glucobar.frontend",
        "CFBundleVersion": "0.3.0",
        "CFBundleShortVersionString": "0.3.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
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
        --dist-dir "${REPO_ROOT}/dist" \
        --bdist-base "${REPO_ROOT}/build/py2app"
)

echo
echo "Build complete:"
echo "  ${REPO_ROOT}/dist/Glucobar.app"
echo
echo "Quick smoke run:"
echo "  open \"${REPO_ROOT}/dist/Glucobar.app\""
