"""Release version helper for non-UI code paths.

``pyproject.toml`` holds the declared version. After install, setuptools may
normalize it in package metadata (for example ``1.0.0`` → ``1.0``). The menubar
About box prefers ``CFBundleShortVersionString`` from the running ``.app``
bundle so the UI matches Finder. Use :func:`package_version` for logs and
OpenAPI when bundle metadata is unavailable.
"""

from __future__ import annotations


def package_version() -> str:
    try:
        from importlib.metadata import version

        return version("gluco")
    except Exception:
        return "1.0.5"
