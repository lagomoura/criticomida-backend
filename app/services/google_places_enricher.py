"""Google Places enrichment service.

Fetches metadata (rating, editorial summary, photos, cuisine types) from the
Places Details API and persists it on the Restaurant row. Designed to be
called either via background task or via an explicit refresh endpoint.

Degrades gracefully when GOOGLE_PLACES_API_KEY is not configured — returns
silently without error.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.restaurant import Restaurant

logger = logging.getLogger(__name__)

PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
PLACES_PHOTO_URL = "https://maps.googleapis.com/maps/api/place/photo"
DETAIL_FIELDS = ",".join(
    [
        "rating",
        "user_ratings_total",
        "editorial_summary",
        "photos",
        "types",
        "opening_hours",
        "current_opening_hours",
    ]
)
MAX_PHOTOS = 6
PHOTO_MAX_WIDTH = 1200
HTTP_TIMEOUT = 15.0
# Restaurant `types` from Google contain many non-cuisine entries
# (food, restaurant, point_of_interest…). Keep only those that look like
# cuisine signals — anything that isn't in this stoplist passes through.
NON_CUISINE_TYPES = {
    "food",
    "restaurant",
    "establishment",
    "point_of_interest",
    "store",
    "meal_takeaway",
    "meal_delivery",
    "lodging",
    "tourist_attraction",
}

# Concurrency limiter for outbound calls
_semaphore = asyncio.Semaphore(4)


def cache_is_fresh(cached_at: datetime | None, ttl_hours: int | None = None) -> bool:
    """True when cached data is still within TTL."""
    if cached_at is None:
        return False
    ttl = ttl_hours or settings.GOOGLE_CACHE_TTL_HOURS
    return datetime.now(timezone.utc) - cached_at < timedelta(hours=ttl)


def _filter_cuisine_types(raw_types: list[str] | None) -> list[str]:
    if not raw_types:
        return []
    return [t for t in raw_types if t not in NON_CUISINE_TYPES]


def _build_photo_url(photo_reference: str, api_key: str) -> str:
    """Direct URL for the Places Photo API. Google handles redirect/serve."""
    return (
        f"{PLACES_PHOTO_URL}?maxwidth={PHOTO_MAX_WIDTH}"
        f"&photo_reference={photo_reference}&key={api_key}"
    )


async def fetch_place_details(
    place_id: str, *, api_key: str
) -> dict | None:
    """Call Places Details API. Returns the `result` dict or None on failure."""
    params = {
        "place_id": place_id,
        "fields": DETAIL_FIELDS,
        "key": api_key,
        "language": "es",
    }
    try:
        async with _semaphore:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(PLACES_DETAILS_URL, params=params)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Places Details fetch failed for %s: %s", place_id, exc)
        return None

    status = body.get("status")
    if status != "OK":
        logger.info(
            "Places Details non-OK status for %s: %s (%s)",
            place_id,
            status,
            body.get("error_message"),
        )
        return None
    return body.get("result")


def _photo_struct(photo: dict, api_key: str) -> dict:
    return {
        "photo_reference": photo.get("photo_reference"),
        "width": photo.get("width"),
        "height": photo.get("height"),
        "attribution_html": (photo.get("html_attributions") or [None])[0],
        "url": _build_photo_url(photo.get("photo_reference", ""), api_key)
        if photo.get("photo_reference")
        else None,
    }


def _apply_details_to_restaurant(
    restaurant: Restaurant, details: dict, *, api_key: str
) -> None:
    rating = details.get("rating")
    if rating is not None:
        restaurant.google_rating = Decimal(str(rating))
    user_total = details.get("user_ratings_total")
    if user_total is not None:
        restaurant.google_user_ratings_total = int(user_total)

    summary = details.get("editorial_summary") or {}
    overview = summary.get("overview")
    if overview:
        restaurant.editorial_summary = overview
        restaurant.editorial_summary_lang = summary.get("language")

    photos_raw = details.get("photos") or []
    if photos_raw:
        restaurant.google_photos = [
            _photo_struct(p, api_key) for p in photos_raw[:MAX_PHOTOS]
        ]

    types = _filter_cuisine_types(details.get("types"))
    if types:
        restaurant.cuisine_types = types

    # Prefer current_opening_hours.weekday_text (richer) but fall back to
    # opening_hours.weekday_text. Only update when we got something — don't
    # wipe existing data on a transient field-missing response.
    weekday_text = (
        (details.get("current_opening_hours") or {}).get("weekday_text")
        or (details.get("opening_hours") or {}).get("weekday_text")
    )
    if weekday_text:
        restaurant.opening_hours = weekday_text

    restaurant.google_cached_at = datetime.now(timezone.utc)


async def refresh_restaurant_from_google(
    db: AsyncSession,
    restaurant_id: uuid.UUID,
    *,
    force: bool = False,
) -> bool:
    """Refresh Google enrichment for a single restaurant.

    Returns True when fields were updated, False otherwise (no key, no place_id,
    cache fresh, or fetch failed).
    """
    api_key = settings.GOOGLE_PLACES_API_KEY
    if not api_key:
        logger.debug("GOOGLE_PLACES_API_KEY not set — skipping enrichment.")
        return False

    result = await db.execute(
        select(Restaurant).where(Restaurant.id == restaurant_id)
    )
    restaurant = result.scalar_one_or_none()
    if restaurant is None or not restaurant.google_place_id:
        return False

    if not force and cache_is_fresh(restaurant.google_cached_at):
        return False

    details = await fetch_place_details(
        restaurant.google_place_id, api_key=api_key
    )
    if details is None:
        return False

    _apply_details_to_restaurant(restaurant, details, api_key=api_key)
    await db.commit()
    return True


async def maybe_schedule_refresh(
    db: AsyncSession, restaurant: Restaurant
) -> None:
    """Best-effort lazy refresh trigger — call from the GET detail endpoint
    via FastAPI BackgroundTasks. Does nothing when key missing / cache fresh."""
    if not settings.GOOGLE_PLACES_API_KEY:
        return
    if not restaurant.google_place_id:
        return
    if cache_is_fresh(restaurant.google_cached_at):
        return
    try:
        await refresh_restaurant_from_google(db, restaurant.id, force=False)
    except Exception:  # pragma: no cover - background task swallow
        logger.exception("Background Google refresh failed")
