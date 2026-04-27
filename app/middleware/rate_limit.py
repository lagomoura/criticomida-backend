"""Rate limiting for social actions (spec §9.2, §16).

Uses slowapi with an in-memory backend. With multiple uvicorn workers each
process keeps its own counters, so the effective limit is `N_workers × limit`.
That's an acceptable trade-off for v1; move to Redis when traffic grows.

Bucketing: authenticated clients share a bucket by user id (regardless of IP),
anonymous clients are bucketed by IP. This matches spec §16 ("por IP y por
usuario").
"""

from __future__ import annotations

from fastapi import Request
from jose import JWTError
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.middleware.auth import decode_jwt_strict


def user_or_ip_key(request: Request) -> str:
    token: str | None = None
    auth_header = request.headers.get("authorization") or request.headers.get(
        "Authorization"
    )
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    else:
        token = request.cookies.get("access_token")

    if token:
        try:
            payload = decode_jwt_strict(token)
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
        except JWTError:
            pass

    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=user_or_ip_key)


# Centralised limit strings — change here and all decorated endpoints follow.
COMMENT_CREATE_LIMIT = "5/minute"
LIKE_LIMIT = "60/minute"
FOLLOW_LIMIT = "30/minute"
POST_CREATE_LIMIT = "10/hour"
REPORT_CREATE_LIMIT = "20/hour"
