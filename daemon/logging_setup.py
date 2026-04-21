"""Configure logging for the daemon: stderr + rotating file.

Kept separate from ``main.py`` so that tests and scripts can reuse the
same configuration without importing FastAPI.

Rotation policy: 1 MB per file, keep 3 backups. Glucose polling at 60s
intervals produces ~1 line per poll at INFO, so 1 MB is roughly a week
of logs. Anything older than that is rarely useful for debugging.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_MAX_BYTES = 1 * 1024 * 1024  # 1 MB
_BACKUP_COUNT = 3


def configure_logging(level: str, log_file: Path | None) -> None:
    """Install stderr + optional rotating-file handlers on the root logger.

    ``force=True`` semantics: remove any handlers added by a previous
    ``basicConfig`` or ``logging.getLogger().addHandler`` call. Important
    because uvicorn and pytest both pre-configure logging, and we want a
    single, predictable pipeline.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001  — handler close failures are benign
            pass

    formatter = logging.Formatter(_FORMAT)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    root.addHandler(stderr)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            log_file,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

    # Quiet down uvicorn's access log; we don't need a line per /health poll.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
