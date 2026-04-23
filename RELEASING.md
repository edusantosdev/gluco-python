# Releasing Glucobar

This runbook covers local build, GitHub release assets, and Homebrew cask updates.

## Prerequisites

- macOS machine with Xcode command-line tools
- Repo checkout with clean working tree (or only intentional release changes)
- Python 3.11+ and local `.venv`
- GitHub permissions to create tags/releases
- Write access to your Homebrew tap repo

## 1) Build and test

From repo root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .[dev,build]
.venv/bin/python -m pytest tests/
scripts/build_portable_app.sh
```

Expected artifact:

- `dist/Glucobar.app`

## 2) Package release zip + SHA256

Choose the version tag (example `v0.3.0`) and run:

```bash
scripts/package_release_artifact.sh --version v0.3.0 --repo eduardo/librelinkup-glucose-menubar-python
```

Outputs:

- `release/Glucobar-v0.3.0-macos.zip`
- `release/Glucobar-v0.3.0-macos.zip.sha256`
- `release/homebrew-cask-snippet.rb`

## 3) Create GitHub tag + release

Create/push the tag and publish the release:

```bash
git tag v0.3.0
git push origin v0.3.0
gh release create v0.3.0 \
  "release/Glucobar-v0.3.0-macos.zip" \
  --title "v0.3.0" \
  --notes "Release notes here."
```

The cask URL must match this pattern exactly:

- `https://github.com/eduardo/librelinkup-glucose-menubar-python/releases/download/v0.3.0/Glucobar-v0.3.0-macos.zip`

## 4) Update Homebrew tap cask

In your tap repo, update `Casks/glucobar.rb`:

- `version` to the new version (without leading `v`)
- `sha256` to the hash from `.sha256`
- keep URL in the `v#{version}`/`Glucobar-v#{version}-macos.zip` format

Reference cask file/template:

- `packaging/homebrew-tap/Casks/glucobar.rb`
- `release/homebrew-cask-snippet.rb`

Commit and push in tap repo:

```bash
git add Casks/glucobar.rb
git commit -m "glucobar 0.3.0"
git push
```

## 5) Verify install from tap

On a clean machine or fresh shell:

```bash
brew update
brew tap <your-org>/<your-tap>
brew install --cask glucobar
```

Then launch once:

```bash
open -a Glucobar
```

## 6) Optional post-release checks

- `brew uninstall --cask glucobar` and reinstall
- `brew upgrade --cask glucobar` from previous version
- Validate Keychain prompt and first-run credentials flow
- Confirm menubar **Run at Startup / Login** toggles correctly

## Notes

- Current artifacts are unsigned and not notarized.
- Before broad distribution, add Developer ID signing + notarization in CI.
