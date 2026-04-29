"""Integration tests for GET /api/restaurants/match-candidates (Fase 2.2)."""

import os
import uuid

import pytest
from sqlalchemy import text

from app.database import engine

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


# Buenos Aires, Av. Corrientes — within Argentina lat/lng range.
BASE_LAT = -34.6037
BASE_LNG = -58.3816


async def _seed_restaurant(
    *, name: str, lat: float, lng: float, place_id: str | None = None
) -> str:
    """Insert a restaurant directly via SQL and return its id."""
    rid = str(uuid.uuid4())
    pid = place_id or f"pytest_place_{uuid.uuid4().hex[:10]}"
    slug = f"pytest-match-{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        creator = await conn.execute(text("SELECT id FROM users LIMIT 1"))
        user_id = creator.scalar_one()
        await conn.execute(
            text(
                "INSERT INTO restaurants "
                "(id, slug, name, location_name, latitude, longitude, "
                " google_place_id, computed_rating, review_count, "
                " created_by, created_at, updated_at) "
                "VALUES (:id, :slug, :name, '', :lat, :lng, :pid, 0, 0, "
                "        :user, now(), now())"
            ),
            {
                "id": rid,
                "slug": slug,
                "name": name,
                "lat": lat,
                "lng": lng,
                "pid": pid,
                "user": user_id,
            },
        )
    return rid


async def _cleanup_match_data() -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM restaurants WHERE slug LIKE 'pytest-match-%'")
        )


@pytest.mark.asyncio
async def test_returns_close_match_with_accent_variation(
    async_client_integration, user_a
):
    """The motivating case: same Spanish name with/without diacritics.
    `unaccent` should let "Pizzeria Guerrin" match "Pizzería Güerrín"."""
    await _seed_restaurant(
        name="Pizzería Güerrín",
        lat=BASE_LAT,
        lng=BASE_LNG,
    )
    try:
        # 30 meters away, accent variation ("Pizzería Güerrín" vs "Pizzeria Guerrin").
        # 30m at this latitude is roughly 0.00027 degrees lat.
        r = await async_client_integration.get(
            "/api/restaurants/match-candidates",
            params={
                "name": "Pizzeria Guerrin",
                "lat": BASE_LAT + 0.00027,
                "lng": BASE_LNG,
            },
            cookies=user_a.cookies,
        )
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "Pizzería Güerrín"
        assert items[0]["distance_m"] < 50.0
        assert items[0]["name_similarity"] >= 0.5
    finally:
        await _cleanup_match_data()


@pytest.mark.asyncio
async def test_excludes_when_distance_too_far(
    async_client_integration, user_a
):
    """Same name 200m away is not a candidate (above 50m threshold)."""
    await _seed_restaurant(
        name="Pizzería Güerrín",
        lat=BASE_LAT,
        lng=BASE_LNG,
    )
    try:
        # 200m away. 0.0018 degrees lat ≈ 200m.
        r = await async_client_integration.get(
            "/api/restaurants/match-candidates",
            params={
                "name": "Pizzería Güerrín",
                "lat": BASE_LAT + 0.0018,
                "lng": BASE_LNG,
            },
            cookies=user_a.cookies,
        )
        assert r.status_code == 200, r.text
        assert r.json()["items"] == []
    finally:
        await _cleanup_match_data()


@pytest.mark.asyncio
async def test_excludes_when_name_too_dissimilar(
    async_client_integration, user_a
):
    """Same coords but very different name is not a candidate."""
    await _seed_restaurant(
        name="Pizzería Güerrín",
        lat=BASE_LAT,
        lng=BASE_LNG,
    )
    try:
        r = await async_client_integration.get(
            "/api/restaurants/match-candidates",
            params={
                "name": "Sushi Pop Argentina",  # totally different name
                "lat": BASE_LAT,
                "lng": BASE_LNG,
            },
            cookies=user_a.cookies,
        )
        assert r.status_code == 200, r.text
        assert r.json()["items"] == []
    finally:
        await _cleanup_match_data()


@pytest.mark.asyncio
async def test_excludes_by_place_id(async_client_integration, user_a):
    """When the user picked a Google Place that already exists by place_id,
    the Fase 2.1 dedup handles it — match-candidates should not also flag it."""
    pid = f"pytest_place_{uuid.uuid4().hex[:10]}"
    await _seed_restaurant(
        name="Pizzería Güerrín",
        lat=BASE_LAT,
        lng=BASE_LNG,
        place_id=pid,
    )
    try:
        r = await async_client_integration.get(
            "/api/restaurants/match-candidates",
            params={
                "name": "Pizzería Güerrín",
                "lat": BASE_LAT,
                "lng": BASE_LNG,
                "exclude_place_id": pid,
            },
            cookies=user_a.cookies,
        )
        assert r.status_code == 200, r.text
        assert r.json()["items"] == []
    finally:
        await _cleanup_match_data()


@pytest.mark.asyncio
async def test_returns_close_match_even_with_different_place_id(
    async_client_integration, user_a
):
    """The whole point of Fase 2.2: same physical place, different place_ids
    in Google. Match should fire even when an exclude_place_id is provided
    but the candidate has a different one."""
    await _seed_restaurant(
        name="La Panera Rosa",
        lat=BASE_LAT,
        lng=BASE_LNG,
        place_id="pytest_place_first",
    )
    try:
        r = await async_client_integration.get(
            "/api/restaurants/match-candidates",
            params={
                "name": "The Panera Rosa",
                "lat": BASE_LAT + 0.00018,  # ~20m
                "lng": BASE_LNG,
                "exclude_place_id": "pytest_place_second",  # different pid
            },
            cookies=user_a.cookies,
        )
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "La Panera Rosa"
        assert items[0]["google_place_id"] == "pytest_place_first"
    finally:
        await _cleanup_match_data()


@pytest.mark.asyncio
async def test_orders_by_confidence_score(async_client_integration, user_a):
    """When multiple candidates pass both thresholds, closer + more similar
    ranks first."""
    # Closer match (10m, identical name)
    await _seed_restaurant(
        name="Café Tortoni",
        lat=BASE_LAT,
        lng=BASE_LNG,
    )
    # Farther + slightly different name (40m, less similar)
    await _seed_restaurant(
        name="Cafe Tortoni Restaurant",
        lat=BASE_LAT + 0.00036,  # ~40m
        lng=BASE_LNG,
    )
    try:
        r = await async_client_integration.get(
            "/api/restaurants/match-candidates",
            params={
                "name": "Café Tortoni",
                "lat": BASE_LAT + 0.00009,  # ~10m
                "lng": BASE_LNG,
            },
            cookies=user_a.cookies,
        )
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 2
        # The closer + identical-name one should rank first.
        assert items[0]["name"] == "Café Tortoni"
        assert items[0]["confidence_score"] > items[1]["confidence_score"]
    finally:
        await _cleanup_match_data()


@pytest.mark.asyncio
async def test_requires_authentication(async_client_integration):
    """No cookies → 401."""
    r = await async_client_integration.get(
        "/api/restaurants/match-candidates",
        params={"name": "Anything", "lat": BASE_LAT, "lng": BASE_LNG},
    )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_validates_lat_lng_bounds(async_client_integration, user_a):
    """Out-of-range lat/lng returns 422."""
    r = await async_client_integration.get(
        "/api/restaurants/match-candidates",
        params={"name": "X", "lat": 999, "lng": -58},
        cookies=user_a.cookies,
    )
    assert r.status_code == 422
