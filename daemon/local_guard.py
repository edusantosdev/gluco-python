"""Refuse HTTP requests that come from web pages rather than local clients.

Binding to 127.0.0.1 keeps other machines out, but not the user's own
browser. Two browser-borne attacks still reach a localhost API:

* **Cross-site requests** — any page can fire a ``POST /auth/refresh``
  (a "simple" request, so no CORS preflight) and force repeated
  LibreView logins, risking rate limits or an account lock.
* **DNS rebinding** — a page on ``evil.example`` re-points its own
  hostname at 127.0.0.1, after which the browser treats the daemon as
  same-origin and lets the page *read* glucose data and the patient name.

The legitimate clients (the menubar's ``httpx`` client, the supervisor's
``urllib`` probe, ``curl``) never send ``Origin`` or ``Sec-Fetch-Site``
and always address the daemon by a loopback name. So we require:

1. ``Host`` names a loopback address (or the configured bind host) —
   defeats rebinding, where ``Host`` carries the attacker's domain.
2. Any ``Origin`` is exactly this daemon (``http://<loopback>:<port>``) —
   defeats cross-site and other-localhost-port requests.
3. Any ``Sec-Fetch-Site`` is ``same-origin`` or ``none`` — belt and
   braces for browser requests that omit ``Origin`` (e.g. no-cors GETs).

Opening ``http://127.0.0.1:8765/docs`` in a browser keeps working: those
requests are same-origin.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Bind addresses that mean "all interfaces" rather than a name clients use.
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def _hostname(value: str) -> str | None:
    """``"[::1]:8765"`` → ``"::1"``; ``"LocalHost"`` → ``"localhost"``."""
    try:
        return urlsplit(f"//{value}").hostname
    except ValueError:
        return None


class LocalOnlyGuard:
    """Pure-ASGI middleware enforcing the rules in the module docstring."""

    def __init__(self, app: ASGIApp, *, port: int, bind_host: str = "127.0.0.1") -> None:
        self.app = app
        self.port = port
        hosts = set(_LOOPBACK_HOSTS)
        bind = (_hostname(bind_host) or "").lower()
        if bind not in _WILDCARD_HOSTS:
            hosts.add(bind)
        self.allowed_hosts = frozenset(hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            reason = self._rejection(Headers(scope=scope))
            if reason is not None:
                log.warning(
                    "Rejected %s %s: %s", scope.get("method"), scope.get("path"), reason
                )
                response = JSONResponse({"detail": reason}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _rejection(self, headers: Headers) -> str | None:
        host = _hostname(headers.get("host", ""))
        if host not in self.allowed_hosts:
            return "Host header is not a local address"

        origin = headers.get("origin")
        if origin is not None and not self._is_own_origin(origin):
            return "Cross-origin requests are not allowed"

        fetch_site = headers.get("sec-fetch-site")
        if fetch_site is not None and fetch_site not in _ALLOWED_FETCH_SITES:
            return "Cross-site requests are not allowed"
        return None

    def _is_own_origin(self, origin: str) -> bool:
        try:
            parts = urlsplit(origin)
            port = parts.port
        except ValueError:
            return False  # also covers the opaque ``Origin: null``
        return (
            parts.scheme == "http"
            and parts.hostname in self.allowed_hosts
            and port == self.port
        )
