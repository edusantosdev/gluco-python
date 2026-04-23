"""``python -m daemon`` entry point.

Reads the bind host/port from settings and hands off to uvicorn.

Missing credentials are caught *here* with exit code 78 (``EX_CONFIG``)
so the frontend's :class:`macos_frontend.daemon_supervisor.DaemonSupervisor`
can distinguish "the user hasn't signed in yet" from a real crash.
Without this, the early ``load_settings()`` call below raises before
``daemon.main`` is ever imported, and uvicorn's default exception
reporting gives us an exit 1 with a traceback — which the supervisor
would then treat as a regular crash and respawn five times a minute.

(``daemon.main`` also has a ``try/except MissingCredentialsError`` at
module load to protect the ``uvicorn daemon.main:app`` entry path;
this second guard covers the ``python -m daemon`` entry.)
"""

from __future__ import annotations

import sys

import uvicorn

from . import parent_watch
from .config import MissingCredentialsError, load_settings


def main() -> None:
    # Arm parent-death detection *before* we bind the port. If the
    # supervisor (the macOS menubar frontend) dies while we're still
    # starting up, we want to release the port + lockfile promptly
    # rather than run indefinitely as a reparented orphan.
    parent_watch.enable_from_env()

    try:
        settings = load_settings()
    except MissingCredentialsError as e:
        print(f"glucose-daemon: {e}", file=sys.stderr)
        sys.exit(78)
    uvicorn.run(
        "daemon.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
