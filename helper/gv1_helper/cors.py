"""Strict GitHub Pages CORS plus Chromium Private Network Access preflights."""
from __future__ import annotations

from collections.abc import Iterable

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .security import github_pages_origin

_ALLOWED_METHODS = {"GET", "POST", "DELETE", "OPTIONS"}
_ALLOWED_HEADERS = {"accept", "content-type", "x-requested-with"}
_EXPOSE_HEADERS = "Content-Disposition, ETag, X-CRC32, X-SHA256"


class BrowserAccessMiddleware:
    """Reject untrusted browser origins and answer CORS/PNA preflights."""

    def __init__(self, app: ASGIApp, explicit_origins: Iterable[str] = ()) -> None:
        self.app = app
        self.explicit_origins = tuple(item.rstrip("/") for item in explicit_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        allowed = bool(origin and github_pages_origin(origin, self.explicit_origins))
        if origin and not allowed:
            response = PlainTextResponse("Origin is not allowed", status_code=403)
            await response(scope, receive, send)
            return

        requested_method = headers.get("access-control-request-method")
        is_preflight = scope["method"] == "OPTIONS" and bool(origin and requested_method)
        if is_preflight:
            method = requested_method.upper()
            requested_headers = {
                item.strip().lower()
                for item in headers.get("access-control-request-headers", "").split(",")
                if item.strip()
            }
            if method not in _ALLOWED_METHODS or not requested_headers <= _ALLOWED_HEADERS:
                response = PlainTextResponse("CORS preflight is not allowed", status_code=400)
            else:
                response_headers = self._cors_headers(origin)
                response_headers["Access-Control-Allow-Methods"] = ", ".join(sorted(_ALLOWED_METHODS))
                response_headers["Access-Control-Allow-Headers"] = ", ".join(sorted(_ALLOWED_HEADERS))
                response_headers["Access-Control-Max-Age"] = "600"
                if headers.get("access-control-request-private-network", "").lower() == "true":
                    response_headers["Access-Control-Allow-Private-Network"] = "true"
                response = Response(status_code=204, headers=response_headers)
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers.setdefault("X-Content-Type-Options", "nosniff")
                response_headers.setdefault("Referrer-Policy", "no-referrer")
                if scope.get("path", "").startswith("/api/"):
                    response_headers.setdefault("Cache-Control", "no-store")
                if allowed and origin:
                    for key, value in self._cors_headers(origin).items():
                        response_headers[key] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _cors_headers(origin: str) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Expose-Headers": _EXPOSE_HEADERS,
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Vary": (
                "Origin, Access-Control-Request-Method, "
                "Access-Control-Request-Headers, Access-Control-Request-Private-Network"
            ),
        }
