"""Security headers middleware for the FastAPI app.

Mirrors the headers we set on the Vercel side (``next.config.ts``).
Both ends of the system get the same baseline so the protections do
not vanish on routes proxied directly to Railway (``/uploads``,
``/api/*`` when called server-to-server).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


_DEFAULT_HEADERS: dict[str, str] = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(self), interest-cohort=()"
    ),
    "X-Frame-Options": "SAMEORIGIN",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds the baseline security headers to every response.

    HSTS is added unconditionally so previews on Railway also pin
    HTTPS. The platform terminates TLS at the edge; the header is
    legitimate even though uvicorn itself speaks plain HTTP behind it.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        for key, value in _DEFAULT_HEADERS.items():
            response.headers.setdefault(key, value)
        return response
