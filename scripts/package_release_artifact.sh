#!/usr/bin/env bash
# Package a portable release artifact from dist/Gluco.app.
#
# Usage:
#   scripts/package_release_artifact.sh [--version vX.Y.Z] [--repo owner/name]
#
# Output:
#   release/Gluco-vX.Y.Z-macos.zip
#   release/Gluco-vX.Y.Z-macos.zip.sha256
#   release/homebrew-cask-snippet.rb

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST_APP="${REPO_ROOT}/dist/Gluco.app"
RELEASE_DIR="${REPO_ROOT}/release"
PYTHON="${REPO_ROOT}/.venv/bin/python"

VERSION=""
GITHUB_REPO=""

usage() {
    echo "usage: $0 [--version vX.Y.Z] [--repo owner/name]" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            [ $# -ge 2 ] || usage
            VERSION="$2"
            shift 2
            ;;
        --repo)
            [ $# -ge 2 ] || usage
            GITHUB_REPO="$2"
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

if [ ! -d "${DIST_APP}" ]; then
    echo "error: ${DIST_APP} not found. Build first:" >&2
    echo "  scripts/build_portable_app.sh" >&2
    exit 1
fi

if [ -z "${VERSION}" ]; then
    if [ ! -x "${PYTHON}" ]; then
        echo "error: .venv python not found and --version not provided." >&2
        echo "  pass --version vX.Y.Z or create .venv first." >&2
        exit 1
    fi
    VERSION="$("${PYTHON}" -c 'import tomllib, pathlib; p = pathlib.Path("pyproject.toml"); d = tomllib.loads(p.read_text(encoding="utf-8")); print("v" + d["project"]["version"])')"
fi

mkdir -p "${RELEASE_DIR}"

ZIP_NAME="Gluco-${VERSION}-macos.zip"
ZIP_PATH="${RELEASE_DIR}/${ZIP_NAME}"
SHA_PATH="${ZIP_PATH}.sha256"

rm -f "${ZIP_PATH}" "${SHA_PATH}"

(
    cd "${REPO_ROOT}/dist"
    ditto -c -k --sequesterRsrc --keepParent "Gluco.app" "${ZIP_PATH}"
)

SHA256="$(shasum -a 256 "${ZIP_PATH}" | awk '{print $1}')"
printf "%s  %s\n" "${SHA256}" "${ZIP_NAME}" > "${SHA_PATH}"

if [ -z "${GITHUB_REPO}" ]; then
    URL_HINT="https://github.com/<owner>/<repo>/releases/download/${VERSION}/${ZIP_NAME}"
else
    URL_HINT="https://github.com/${GITHUB_REPO}/releases/download/${VERSION}/${ZIP_NAME}"
fi

CASK_SNIPPET="${RELEASE_DIR}/homebrew-cask-snippet.rb"
cat > "${CASK_SNIPPET}" <<EOF
cask "gluco" do
  version "${VERSION#v}"
  sha256 "${SHA256}"

  url "${URL_HINT}"
  name "Gluco"
  desc "macOS menubar glucose display backed by LibreView"
  homepage "https://github.com/${GITHUB_REPO:-<owner>/<repo>}"

  app "Gluco.app"

  zap trash: [
    "~/.glucobar",
    "~/Library/LaunchAgents/com.glucobar.frontend.plist",
  ]
end
EOF

echo "Packaged release artifact:"
echo "  ${ZIP_PATH}"
echo "  ${SHA_PATH}"
echo
echo "Generated Homebrew cask snippet:"
echo "  ${CASK_SNIPPET}"
