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


limiter = Limiter(key_func=user_or_ip_key, key_style="endpoint")
# key_style="endpoint" agrupa todas las requests al mismo view function en
# un único bucket (ej. follow_user). Con el default "url", cada path
# resolvido es un bucket distinto — eso significaba que un user podía
# seguir a N personas distintas sin gastar el budget de 30/min, porque
# /api/users/uuid1/follow y /api/users/uuid2/follow caían en buckets
# separados. Confirmado leyendo slowapi.Limiter._check_request_limit.


# Centralised limit strings — change here and all decorated endpoints follow.
COMMENT_CREATE_LIMIT = "5/minute"
LIKE_LIMIT = "60/minute"
FOLLOW_LIMIT = "30/minute"
POST_CREATE_LIMIT = "10/hour"
REPORT_CREATE_LIMIT = "20/hour"
CLAIM_CREATE_LIMIT = "3/day"

# Auth — bucketed mostly by IP since the actor is pre-auth. Tight on the
# pre-auth surface (login / forgot / reset / verify) to slow down brute
# force and email-flood attacks; looser on post-auth resend so a
# legitimate user retrying twice doesn't trip it.
AUTH_LOGIN_LIMIT = "10/minute"
AUTH_REGISTER_LIMIT = "5/hour"
AUTH_FORGOT_PASSWORD_LIMIT = "5/hour"
AUTH_RESET_PASSWORD_LIMIT = "10/hour"
AUTH_REFRESH_LIMIT = "60/minute"
AUTH_VERIFY_EMAIL_LIMIT = "20/hour"
AUTH_RESEND_VERIFICATION_LIMIT = "5/hour"

# IA / LLM — chat stream and ghostwriter assist talk to paid providers.
# Cap aggressively per (user|ip) to keep cost runaway out of the threat
# model. Anonymous Sommelier traffic shares ``CHAT_STREAM_LIMIT`` keyed
# by IP via ``user_or_ip_key``; the limit applies regardless.
CHAT_STREAM_LIMIT = "30/hour"
GHOSTWRITER_ASSIST_LIMIT = "20/hour"

# Map discovery — heavy aggregate query (geek_score/value_prop/trending
# over all dishes in bbox). FE uses a 300ms debounce + AbortController,
# so a single legit session emits ~20-40 req/min during active pan.
# 120/min gives a 3-6× buffer for legit users and caps scripted abuse.
MAP_BBOX_LIMIT = "120/minute"
