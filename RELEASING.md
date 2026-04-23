# Releasing Glucobar

Short release checklist for macOS artifacts and Homebrew cask updates.
Release automation lives in `.github/workflows/release.yml`.

## 1. Build and test locally

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .[dev,build]
.venv/bin/python -m pytest tests/
.venv/bin/python -m ruff check .
scripts/build_portable_app.sh
```

Output: `dist/Glucobar.app`.

## 2. Package a release artifact

```bash
scripts/package_release_artifact.sh --version vX.Y.Z --repo <owner>/<repo>
```

Outputs under `release/`:

- `Glucobar-vX.Y.Z-macos.zip`
- `Glucobar-vX.Y.Z-macos.zip.sha256`
- `homebrew-cask-snippet.rb`

Without `--version`, the script reads the version from `pyproject.toml`.

## 3. Publish to GitHub

Tag-triggered workflow:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

This runs lint + tests, builds the app, packages the zip/SHA, and
uploads all three artifacts to the GitHub release for the tag.

You can also trigger the workflow manually via
`workflow_dispatch` (`inputs.version`).

## 4. Update the Homebrew tap cask

In your tap, edit `Casks/glucobar.rb`:

- `version "X.Y.Z"`
- `sha256` from the release `.sha256` file
- URL must resolve to:
  `https://github.com/<owner>/<repo>/releases/download/vX.Y.Z/Glucobar-vX.Y.Z-macos.zip`

Commit and push in the tap repo.

## 5. Verify install

```bash
brew update
brew tap <your-org>/<your-tap>
brew install --cask glucobar
open -a Glucobar
```

Optionally `brew upgrade --cask glucobar` from the previous version.

## Notes

- Artifacts are currently unsigned and not notarized.
- For a manual fallback (no tag workflow), use
  `gh release create vX.Y.Z release/Glucobar-vX.Y.Z-macos.zip`.
