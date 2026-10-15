#!/usr/bin/env bash
# Compile the Icon Composer icon (``scripts/app_bundle/Glucobar.icon``)
# into an ``Assets.car`` for the .app's ``Contents/Resources``.
#
# Usage:
#   scripts/compile_app_icon.sh <output-dir>
#
# Writes ``<output-dir>/Assets.car``, which carries the Liquid Glass icon
# (default / dark / clear / tinted) that macOS 26+ looks up via
# ``CFBundleIconName``. ``Glucobar.icns`` (``CFBundleIconFile``) stays the
# fallback for older macOS.
#
# Needs ``actool`` from a full Xcode 26+ (Command Line Tools alone don't
# ship it). Exits 1 without writing anything when it's unavailable, so
# callers can skip it and keep the ``.icns``-only bundle.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ICON_SRC="${REPO_ROOT}/scripts/app_bundle/Glucobar.icon"

if [ $# -ne 1 ]; then
    echo "usage: $0 <output-dir>" >&2
    exit 2
fi
OUT_DIR="$1"

# ``xcode-select`` often points at the Command Line Tools, which lack
# ``actool``; fall back to the default Xcode location in that case.
if ! xcrun --find actool >/dev/null 2>&1; then
    if [ -d /Applications/Xcode.app/Contents/Developer ]; then
        export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
    fi
fi
if ! xcrun --find actool >/dev/null 2>&1; then
    echo "warn: actool not found (needs Xcode 26+); skipping Icon Composer icon." >&2
    exit 1
fi

TMP_OUT="$(mktemp -d)"
trap '/bin/rm -rf "${TMP_OUT}"' EXIT

# ``--minimum-deployment-target`` below 26 makes actool also emit
# pre-Liquid-Glass renditions inside ``Assets.car``. actool writes a
# ``Glucobar.icns`` too; we keep the repo's own ``.icns`` instead.
if ! xcrun actool "${ICON_SRC}" \
    --compile "${TMP_OUT}" \
    --platform macosx \
    --target-device mac \
    --minimum-deployment-target 11.0 \
    --app-icon Glucobar \
    --output-partial-info-plist "${TMP_OUT}/partial.plist" \
    --output-format human-readable-text \
    --errors --warnings; then
    echo "warn: actool failed to compile ${ICON_SRC}; skipping Icon Composer icon." >&2
    exit 1
fi

mkdir -p "${OUT_DIR}"
cp -f "${TMP_OUT}/Assets.car" "${OUT_DIR}/Assets.car"
echo "Wrote ${OUT_DIR}/Assets.car"
