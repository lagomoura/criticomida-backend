"""Integration tests for the Ghostwriter assist endpoints.

Covers the full contract of both variants:

* multipart upload: 200 happy path, 401 sin auth, 400 con bytes
  inválidos, 422 sólo cuando ``dish_id`` no es UUID (no en otros
  campos), filtrado de ``new_tags`` contra ``draft_text``.
* JSON con URL: 200 con ``photo_url``, 400 sin photo_url ni dish_id.

Caveat: la regresión que motivó este archivo (``Annotated[..., File()]``
bajo ``@limiter.limit`` devolviendo 422 ``query.X: Field required``)
sólo se reproduce en producción (Railway). En local Docker la
introspección de FastAPI funciona aun con el patrón buggy, así que
estos tests no la atrapan — quedan como guard contra regresiones
generales del wiring (auth, multipart, validador, shape de respuesta)
y como documentación ejecutable del contrato.

El servicio de visión está mockeado — los tests son sobre wiring, no
sobre el comportamiento del proveedor.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


# 12-byte PNG header — passes ``assert_image_or_raise`` without needing
# a full encoded image. The vision call is mocked, so the rest of the
# bytes are never inspected.
_PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00"


_FAKE_RAW: dict[str, Any] = {
    "tags": ["picante", "vegetariano"],
    "visible_ingredients": ["tomate", "albahaca"],
    "plating_style": "minimalista",
    "editorial_blurb": "Un plato simple y bien ejecutado.",
    "suggested_pros": ["fresco"],
    "suggested_cons": ["porción chica"],
}


@pytest.fixture
def _mock_vision(monkeypatch):
    """Patch the vision provider where the router imports it from."""

    async def fake_analyze(**_kwargs):
        return _FAKE_RAW

    monkeypatch.setattr(
        "app.routers.ghostwriter.analyze_dish_photo", fake_analyze
    )


@pytest.mark.asyncio
async def test_assist_upload_happy_path(
    async_client_integration, user_a, _mock_vision
):
    """Multipart with photo only — exercises the regression path that
    used to return 422 ``query.photo: Field required``."""
    files = {"photo": ("dish.png", _PNG_HEADER, "image/png")}
    r = await async_client_integration.post(
        "/api/dish-reviews/assist/upload",
        files=files,
        cookies=user_a.cookies,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] == ["picante", "vegetariano"]
    assert body["plating_style"] == "minimalista"
    assert body["editorial_blurb"] == "Un plato simple y bien ejecutado."
    # ``new_tags`` is computed server-side: with no draft, all tags are new.
    assert body["new_tags"] == ["picante", "vegetariano"]


@pytest.mark.asyncio
async def test_assist_upload_filters_new_tags_against_draft(
    async_client_integration, user_a, _mock_vision
):
    """``new_tags`` excludes anything the user already typed in the draft."""
    files = {"photo": ("dish.png", _PNG_HEADER, "image/png")}
    data = {"draft_text": "Me encantó lo picante del plato."}
    r = await async_client_integration.post(
        "/api/dish-reviews/assist/upload",
        files=files,
        data=data,
        cookies=user_a.cookies,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] == ["picante", "vegetariano"]
    # "picante" already in the draft → excluded from new_tags.
    assert body["new_tags"] == ["vegetariano"]


@pytest.mark.asyncio
async def test_assist_upload_rejects_invalid_image(
    async_client_integration, user_a, _mock_vision
):
    """Non-image bytes get a 400, not a 422 — proves the validator runs
    and the multipart parameters are wired correctly."""
    files = {"photo": ("not-an-image.txt", b"hello world", "image/png")}
    r = await async_client_integration.post(
        "/api/dish-reviews/assist/upload",
        files=files,
        cookies=user_a.cookies,
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_assist_upload_requires_auth(async_client_integration):
    """Anonymous → 401, never 422. Confirms the dependency is in scope."""
    files = {"photo": ("dish.png", _PNG_HEADER, "image/png")}
    r = await async_client_integration.post(
        "/api/dish-reviews/assist/upload",
        files=files,
    )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_assist_upload_rejects_invalid_dish_id(
    async_client_integration, user_a, _mock_vision
):
    """A non-UUID ``dish_id`` should yield a 422 on *that* field, not
    bleed through the validator due to a misrouted parameter."""
    files = {"photo": ("dish.png", _PNG_HEADER, "image/png")}
    data = {"dish_id": "not-a-uuid"}
    r = await async_client_integration.post(
        "/api/dish-reviews/assist/upload",
        files=files,
        data=data,
        cookies=user_a.cookies,
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list)
    # The error must be about ``dish_id`` specifically — not about the
    # photo or the dependencies, which would indicate the regression.
    locs = [".".join(str(p) for p in (e.get("loc") or [])) for e in detail]
    assert any("dish_id" in loc for loc in locs), locs
    assert not any("photo" in loc or loc.endswith("db") or loc.endswith("user") for loc in locs), locs


@pytest.mark.asyncio
async def test_assist_url_requires_photo_url_or_dish_id(
    async_client_integration, user_a, _mock_vision
):
    """JSON variant: empty body → 400 (the route raises explicitly).
    Mostly here to keep the JSON path covered alongside the multipart one."""
    r = await async_client_integration.post(
        "/api/dish-reviews/assist",
        json={},
        cookies=user_a.cookies,
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_assist_url_with_photo_url(
    async_client_integration, user_a, _mock_vision
):
    """JSON variant: photo_url + draft_text round-trip."""
    payload = {
        "photo_url": "https://example.com/dish.jpg",
        "draft_text": "El plato venía con tomate y albahaca.",
    }
    r = await async_client_integration.post(
        "/api/dish-reviews/assist",
        json=payload,
        cookies=user_a.cookies,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] == ["picante", "vegetariano"]
    # Both tags absent from the draft → both new.
    assert body["new_tags"] == ["picante", "vegetariano"]
