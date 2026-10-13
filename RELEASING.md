# Releasing GlucoBar

Short release checklist for macOS artifacts and Homebrew cask updates.
Release automation lives in `.github/workflows/release.yml`.

Set `version` in `pyproject.toml` to the release you are shipping (for example
`1.0.0`), and update `scripts/app_bundle/Info.plist.tmpl` and
`scripts/build_portable_app.sh` if those strings still need to match. Reinstall
or rebuild app bundles so Finder and the About box show the new version.

## 1. Build and test locally

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,build]'
.venv/bin/python -m pytest tests/
.venv/bin/python -m ruff check .
scripts/build_portable_app.sh
```

Output: `dist/GlucoBar.app`.

## 2. Package a release artifact

```bash
scripts/package_release_artifact.sh --version vX.Y.Z --repo <owner>/<repo>
```

Outputs under `release/`:

- `GlucoBar-vX.Y.Z-macos.zip`
- `GlucoBar-vX.Y.Z-macos.zip.sha256`
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

## 4. Update the Homebrew cask

The cask lives in this repo at `Casks/gluco-bar.rb` (the repo doubles as its
own tap). Edit it:

- `version "X.Y.Z"`
- `sha256` from the release `.sha256` file
- URL must resolve to:
  `https://github.com/edusantosdev/glucobar-python/releases/download/vX.Y.Z/GlucoBar-vX.Y.Z-macos.zip`

Commit and push to `main`.

## 5. Verify install

```bash
brew tap edusantosdev/glucobar https://github.com/edusantosdev/glucobar-python
brew update
brew install --cask gluco-bar
open -a GlucoBar
```

Optionally `brew upgrade --cask gluco-bar` from the previous version.

## Notes

- Artifacts are currently unsigned and not notarized.
- For a manual fallback (no tag workflow), use
  `gh release create vX.Y.Z release/GlucoBar-vX.Y.Z-macos.zip`.
