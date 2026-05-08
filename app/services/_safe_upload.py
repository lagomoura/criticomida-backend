"""Validation helpers for user-uploaded files.

Avoids two classes of mistakes:

1. Trusting the ``content_type`` header that the client sends — it is
   purely advisory and can be anything. We sniff magic bytes instead.
2. Trusting the ``filename`` extension. Same reason: a ``.png`` may be
   an HTML file with embedded JavaScript, and serving it from our
   origin would give the attacker XSS-via-Storage.

The whitelist below is intentionally narrow: JPEG, PNG, WebP. HEIC is
*not* allowed despite being common on iOS — the upload pipeline does
not currently re-encode it server-side, so a HEIC uploaded today is
served as HEIC tomorrow, and Safari is the only browser that renders
it correctly. Keep the surface tight; expand when re-encoding lands.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DetectedImage:
    """Result of sniffing the first bytes of an uploaded file."""

    mime: str
    extension: str  # leading dot, e.g. ".jpg"


# Maximum bytes we ever read into memory for a user upload. The current
# review flow encodes one image at a time and Gemini caps inputs around
# 20 MB. 8 MB stays well below that and keeps memory pressure modest
# even under fan-in.
DEFAULT_MAX_UPLOAD_BYTES: int = 8 * 1024 * 1024


def detect_image_type(data: bytes) -> DetectedImage | None:
    """Return ``DetectedImage`` if ``data`` starts with a known image
    signature, else ``None``.

    Only checks the first ~12 bytes — enough to identify all formats
    we accept. Designed to be cheap to call before we ever write to
    disk or hand bytes to a downstream AI provider.
    """
    if len(data) < 12:
        return None

    # JPEG: ``FF D8 FF``
    if data[:3] == b"\xff\xd8\xff":
        return DetectedImage(mime="image/jpeg", extension=".jpg")

    # PNG: ``89 50 4E 47 0D 0A 1A 0A``
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return DetectedImage(mime="image/png", extension=".png")

    # WebP: ``RIFF .... WEBP``
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return DetectedImage(mime="image/webp", extension=".webp")

    return None


def assert_image_or_raise(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> DetectedImage:
    """Validate ``data`` is a supported image and below the size cap.

    Raises ``ValueError`` with a short message on failure. Callers
    should wrap into the framework-appropriate HTTPException — this
    helper stays framework-agnostic so it can be reused from background
    jobs / scripts.
    """
    if not data:
        raise ValueError("empty file")
    if len(data) > max_bytes:
        raise ValueError(
            f"file too large ({len(data)} bytes > cap {max_bytes})"
        )
    detected = detect_image_type(data)
    if detected is None:
        raise ValueError("unsupported image type")
    return detected
