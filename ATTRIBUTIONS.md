# Open source and third-party acknowledgments

Gluco is distributed under the **Apache License 2.0**. See `LICENSE` and `NOTICE`.

## App icon (graphic asset)

The droplet shape used for `Gluco.icns` and the Icon Composer icon `Gluco.icon` is from **`scripts/app_bundle/drop-svgrepo-com.svg`**, the [“Drop” icon on SVG Repo](https://www.svgrepo.com/svg/478761/drop) (SVG Repo #478761). SVG Repo lists this asset under its [Public Domain (PD) license](https://www.svgrepo.com/page/licensing/#PD), so it can be used, modified, and redistributed without attribution; it is credited here as a courtesy.

`scripts/build_gluco_icns.py` rasterizes that path with AppKit and runs Apple’s `iconutil` to produce the `.icns` file.

`scripts/app_bundle/Gluco.icon/Assets/drop.svg` is the same path, filled white and placed on a 1024×1024 canvas. `scripts/compile_app_icon.sh` compiles the `.icon` with Apple’s `actool` into the bundle’s `Assets.car` (the Liquid Glass icon on macOS 26+).

## macOS & Apple frameworks

- **AppKit** and **Foundation** (system frameworks), accessed from Python through **PyObjC** (`pyobjc-framework-Cocoa` and related packages, MIT License).

- **`iconutil`** (Xcode Command Line Tools) — builds `.icns` from a `.iconset`.

- **`actool`** (Xcode 26+) — compiles the Icon Composer `.icon` into `Assets.car`. Optional: without it the app ships with the `.icns` only.

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
