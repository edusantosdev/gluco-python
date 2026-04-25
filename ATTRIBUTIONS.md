# Open source and third-party acknowledgments

GlucoBar is distributed under the **Apache License 2.0**. See `LICENSE` and `NOTICE`.

## App icon (graphic asset)

The droplet shape used for `GlucoBar.icns` is from **`scripts/app_bundle/drop-svgrepo-com.svg`**, published via [SVG Repo](https://www.svgrepo.com/). Licensing varies by individual asset; confirm the terms that apply to this file on SVG Repo or the original artist page before you redistribute.

`scripts/build_glucobar_icns.py` rasterizes that path with AppKit and runs Apple’s `iconutil` to produce the `.icns` file.

## macOS & Apple frameworks

- **AppKit** and **Foundation** (system frameworks), accessed from Python through **PyObjC** (`pyobjc-framework-Cocoa` and related packages, MIT License).

- **`iconutil`** (Xcode Command Line Tools) — builds `.icns` from a `.iconset`.

## Python — direct runtime dependencies

These are the packages declared in `pyproject.toml` (see each project for its full license text):

| Project | SPDX (typical) | Role |
|--------|----------------|------|
| [pylibrelinkup](https://pypi.org/project/pylibrelinkup/) | MIT | LibreView / LibreLinkUp API client |
| [rumps](https://pypi.org/project/rumps/) | BSD-3-Clause | macOS menubar |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | BSD-3-Clause | `.env` loading |
| [keyring](https://pypi.org/project/keyring/) | MIT / PSF-2.0 (platform-dependent backends) | macOS Keychain access |
| [FastAPI](https://pypi.org/project/fastapi/) | MIT | Daemon HTTP API |
| [Uvicorn](https://pypi.org/project/uvicorn/) | BSD-3-Clause | ASGI server |
| [Pydantic](https://pypi.org/project/pydantic/) | MIT | Settings and models |
| [pydantic-settings](https://pypi.org/project/pydantic-settings/) | MIT | Typed settings |
| [httpx](https://pypi.org/project/httpx/) | BSD-3-Clause | HTTP client (frontend → daemon) |
| PyObjC (e.g. `pyobjc-framework-Cocoa`, `pyobjc-core`) | MIT | Python ↔ Objective-C bridge |

Transitive dependencies (dependencies of the above) are also subject to their own licenses. For a full installed tree, run `pip list` or a license report tool (e.g. `pip-licenses`) in your environment.

## Build and release tooling (development)

- **setuptools**, **wheel** — build backend (`pyproject.toml` `[build-system]`).
- **py2app** — macOS app bundle (`.app`) in `scripts/build_portable_app.sh`.
- **pytest** — tests.
- **ruff** — linting.

## Optional / platform tooling

- **Homebrew** cask or tap workflows (see `RELEASING.md`) are separate from this repository’s license; follow Homebrew and GitHub’s terms when publishing.

If you add a new bundled asset, dependency, or tool, update this file and `NOTICE` as appropriate so credit and license terms stay traceable.
