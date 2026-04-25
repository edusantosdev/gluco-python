# Third-Party Notices

GlucoBar is licensed under Apache-2.0. It also uses third-party software and
assets under their own licenses. This file is an acknowledgement list for the
main runtime, build, test, and asset dependencies used by the project.

For exact license text, consult each upstream project and the packages installed
in the Python environment or bundled application.

## Runtime Software

- `pylibrelinkup` — LibreView client library, MIT License.
- `rumps` — macOS menu bar app helper, BSD License.
- `pyobjc-core` and `pyobjc-framework-Cocoa` — Python bridge to AppKit and
  Foundation, MIT License.
- `python-dotenv` — `.env` loading, BSD-3-Clause License.
- `keyring` — Keychain credential storage bridge, MIT License.
- `fastapi` — local daemon HTTP API framework, MIT License.
- `uvicorn` — local ASGI server, BSD-3-Clause License.
- `pydantic` and `pydantic-core` — data validation and settings models, MIT
  License.
- `pydantic-settings` — typed settings support, MIT License.
- `httpx` and `httpcore` — HTTP client stack, BSD-3-Clause License.
- `starlette` — ASGI toolkit used by FastAPI, BSD-3-Clause License.
- `anyio` — async compatibility layer, MIT License.
- `h11` — HTTP/1.1 protocol library, MIT License.
- `certifi` — certificate bundle, MPL-2.0.
- `idna`, `click`, `typing-extensions`, `annotated-types`, `sniffio`, and
  `jaraco.*` packages — supporting Python libraries used directly or
  transitively by the runtime stack.

## Build And Development Tools

- `py2app` — builds the standalone macOS `.app`, MIT or PSF License.
- `setuptools` and `wheel` — Python packaging/build tools.
- `pytest` — test runner, MIT License.
- `ruff` — linter, MIT License.
- Apple `iconutil`, AppKit, Foundation, LaunchServices, and related macOS
  system frameworks are used when building and running on macOS.

## Visual Assets

- App icon droplet: "Drop SVG Vector" from SVG Repo,
  <https://www.svgrepo.com/svg/99725/drop>. Listed by SVG Repo as CC0.
  The SVG is stored at `scripts/app_bundle/drop-svgrepo-com.svg` and rendered
  into `GlucoBar.icns` by `scripts/build_glucobar_icns.py`.

## Trademarks

"FreeStyle Libre", "LibreLink", "LibreLinkUp", "LibreView", and related Abbott
marks are trademarks of Abbott Laboratories. GlucoBar is independent and is not
affiliated with, endorsed by, or supported by Abbott or LibreView.
