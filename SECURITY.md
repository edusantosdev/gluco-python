# Security Policy

## Supported versions

Only the latest release of Glucobar receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability**. Include steps to reproduce, the affected
version, and the impact you expect.

You should get an acknowledgement within a week. Once a fix is released, the
advisory will be published with credit to the reporter unless you prefer
otherwise.

## Scope

Areas of particular interest:

- Handling of LibreView credentials (macOS Keychain, the environment handed to
  the daemon process, the cached session token under `~/.glucobar/`).
- The localhost daemon on `127.0.0.1:8765`, including the browser-request guard
  in `daemon/local_guard.py`.
- The packaged `.app` bundle and release artifacts.

Issues in LibreView itself or in upstream dependencies should be reported to
their respective maintainers.
