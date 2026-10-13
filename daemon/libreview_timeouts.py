"""Give every ``pylibrelinkup`` HTTP call a timeout.

``pylibrelinkup`` calls ``requests.get`` / ``requests.post`` without a
``timeout``, and ``requests`` defaults to waiting forever. A LibreView
server that accepts the connection but never answers would therefore
hang the poller thread indefinitely — while it holds the service's
client lock, so ``POST /auth/refresh`` would hang behind it too.

The library offers no hook for this, so we replace the ``requests``
reference *inside the library's module* with a thin proxy that adds a
default timeout. Nothing else in the process is affected: other code
importing ``requests`` still gets the real module.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any

import pylibrelinkup.pylibrelinkup as _llu_module

log = logging.getLogger(__name__)

# ``(connect, read)`` seconds. Generous enough for a slow LibreView login,
# short enough that a hung server is noticed well within one poll interval.
LIBREVIEW_TIMEOUT: tuple[float, float] = (10.0, 30.0)


class _TimeoutRequests:
    """Proxy for the ``requests`` module that defaults ``timeout=``."""

    def __init__(self, module: ModuleType, timeout: tuple[float, float]) -> None:
        self._module = module
        self._timeout = timeout

    def request(self, method: str, url: str, **kwargs: Any):
        kwargs.setdefault("timeout", self._timeout)
        return self._module.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.request("POST", url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Exceptions, Session, etc. — anything we don't wrap.
        return getattr(self._module, name)


def install(timeout: tuple[float, float] = LIBREVIEW_TIMEOUT) -> bool:
    """Wrap ``pylibrelinkup``'s ``requests`` with a default timeout.

    Idempotent. Returns False (and logs) if a future library version no
    longer reaches HTTP through a module-level ``requests`` import, so the
    patch can't silently go stale without a trace in the daemon log.
    """
    current = getattr(_llu_module, "requests", None)
    if isinstance(current, _TimeoutRequests):
        return True
    if not isinstance(current, ModuleType):
        log.warning(
            "pylibrelinkup no longer imports `requests` as a module; "
            "LibreView calls will run without a timeout"
        )
        return False
    _llu_module.requests = _TimeoutRequests(current, timeout)
    return True
