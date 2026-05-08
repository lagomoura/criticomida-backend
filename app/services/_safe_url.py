"""SSRF-safe HTTP fetch helpers.

Used by anywhere the backend has to dereference a user-controlled URL —
today that is the vision pipeline (Ghostwriter assist, Sommelier
``identify_dish_from_photo``). Centralising the rules so only one place
needs to evolve when we add a new sink.

Threat model and trade-offs are spelled out in
``docs/security_safe_url.md`` once the doc lands; the short version:

- Only ``http`` / ``https`` schemes are accepted.
- The hostname is resolved up-front and every resolved IP is checked
  against a denylist of private, loopback, link-local, multicast and
  reserved ranges. If any resolved IP is in those ranges, the request
  is rejected.
- Redirects are NOT followed. A 3xx response is treated as an error.
  This is intentional: validating the redirect chain is bug-prone and
  most real CDN URLs we see in the wild are direct-200.
- Responses are size-capped (``max_bytes``) to keep an attacker from
  serving a 10 GB stream that holds the function hostage.
- DNS rebinding (resolve-then-connect race) is unmitigated; the short
  per-request timeout caps the worst-case impact.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx


class UnsafeURLError(ValueError):
    """Raised when a URL is rejected before we ever connect to it."""


_ALLOWED_SCHEMES = {"http", "https"}


def _looks_like_ip_literal(host: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _ip_is_disallowed(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _resolve_host(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve ``host`` to all of its A/AAAA records.

    Runs ``getaddrinfo`` in a thread to keep the event loop free.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(
        None, socket.getaddrinfo, host, None
    )
    seen: set[str] = set()
    addresses: list[ipaddress._BaseAddress] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            addresses.append(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    return addresses


async def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("missing host")

    literal = _looks_like_ip_literal(host)
    if literal is not None:
        if _ip_is_disallowed(literal):
            raise UnsafeURLError(f"ip in denied range: {host}")
        return

    try:
        resolved = await _resolve_host(host)
    except (socket.gaierror, OSError) as exc:
        raise UnsafeURLError(f"dns resolution failed for {host!r}") from exc
    if not resolved:
        raise UnsafeURLError(f"dns resolution returned no addresses for {host!r}")
    for ip in resolved:
        if _ip_is_disallowed(ip):
            raise UnsafeURLError(
                f"resolved ip in denied range: {host} -> {ip}"
            )


async def safe_fetch_bytes(
    url: str,
    *,
    timeout: float = 10.0,
    max_bytes: int = 16 * 1024 * 1024,
) -> tuple[bytes, str]:
    """Fetch ``url`` and return ``(content, mime_type)``.

    Raises ``UnsafeURLError`` if the URL is rejected before connecting.
    Raises ``httpx.HTTPError`` on transport / status errors. Raises
    ``UnsafeURLError`` if the response body exceeds ``max_bytes`` or the
    server tries to redirect.
    """
    await _validate_url(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        async with c.stream("GET", url) as response:
            if 300 <= response.status_code < 400:
                raise UnsafeURLError(
                    f"redirect blocked: {response.status_code} -> "
                    f"{response.headers.get('location', '?')}"
                )
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    declared = 0
                if declared > max_bytes:
                    raise UnsafeURLError(
                        f"declared size {declared} exceeds cap {max_bytes}"
                    )
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > max_bytes:
                    raise UnsafeURLError(
                        f"response exceeded cap {max_bytes} bytes"
                    )
                chunks.append(chunk)
            mime = (
                response.headers.get("content-type", "application/octet-stream")
                .split(";")[0]
                .strip()
                or "application/octet-stream"
            )
            return b"".join(chunks), mime
