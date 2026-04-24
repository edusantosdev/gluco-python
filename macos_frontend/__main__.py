"""Entry point for both ``python -m macos_frontend`` and py2app bundles.

Use an absolute import so execution also works when py2app runs this file as a
top-level ``__main__`` script (where package-relative imports are invalid).
"""

from macos_frontend.app import main

if __name__ == "__main__":
    raise SystemExit(main())
