"""Backfill Google Places enrichment data for existing restaurants.

Two-stage:
  1. For restaurants WITHOUT google_place_id, query Find Place API by
     `<name> <location>` and persist the resolved place_id.
  2. For restaurants WITH place_id but no fresh google_cached_at, run the
     enrichment service (rating, photos, summary, types).

Usage (from backend/ directory):
    # Dry-run (default — only logs what would happen, no DB writes, no API calls):
    python scripts/backfill_google_places.py

    # Apply for real (calls Places API; counts toward billing):
    python scripts/backfill_google_places.py --commit

    # Skip stage 1 (only re-enrich restaurants that already have place_id):
    python scripts/backfill_google_places.py --commit --skip-find-place

    # Limit how many restaurants to process per stage (useful for sampling):
    python scripts/backfill_google_places.py --commit --limit 10

Requires GOOGLE_PLACES_API_KEY env var (read via app.config.settings).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models.restaurant import Restaurant
from app.services.google_places_enricher import refresh_restaurant_from_google

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_google_places")

FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"


async def find_place_id(
    client: httpx.AsyncClient, name: str, location_hint: str | None
) -> str | None:
    query = f"{name} {location_hint}".strip() if location_hint else name
    params = {
        "input": query,
        "inputtype": "textquery",
        "fields": "place_id,name,formatted_address",
        "key": settings.GOOGLE_PLACES_API_KEY,
        "language": "es",
    }
    try:
        resp = await client.get(FIND_PLACE_URL, params=params)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Find Place request failed for %s: %s", name, exc)
        return None

    status = body.get("status")
    candidates = body.get("candidates") or []
    if status != "OK" or not candidates:
        if status not in ("ZERO_RESULTS", "OK"):
            logger.warning(
                "Find Place non-OK status for %s: %s (%s)",
                name,
                status,
                body.get("error_message"),
            )
        return None
    top = candidates[0]
    logger.debug(
        "  candidate: %s @ %s (place_id=%s)",
        top.get("name"),
        top.get("formatted_address"),
        top.get("place_id"),
    )
    return top.get("place_id")


async def stage_find_place_ids(
    db: AsyncSession, *, commit: bool, limit: int | None
) -> int:
    stmt = (
        select(Restaurant)
        .where(Restaurant.google_place_id.is_(None))
        .where(~Restaurant.slug.like("%test%"))
        .where(~Restaurant.slug.like("pytest%"))
        .where(~Restaurant.slug.like("legacy%"))
        .order_by(Restaurant.created_at)
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    logger.info("Stage 1: %d restaurants without place_id", len(rows))

    if not rows:
        return 0

    resolved = 0
    async with httpx.AsyncClient(timeout=15.0) as client:
        for r in rows:
            location = r.city or r.location_name
            logger.info("  → %s (%s)", r.name, location[:60] if location else "—")
            if not commit:
                continue
            place_id = await find_place_id(client, r.name, location)
            if place_id:
                r.google_place_id = place_id
                logger.info("    ✓ place_id=%s", place_id)
                resolved += 1
            else:
                logger.info("    ✗ no candidate")
            await asyncio.sleep(0.15)  # gentle pacing

    if commit:
        await db.commit()
    return resolved


async def stage_enrich(
    db: AsyncSession, *, commit: bool, limit: int | None
) -> int:
    stmt = (
        select(Restaurant)
        .where(Restaurant.google_place_id.is_not(None))
        .order_by(Restaurant.google_cached_at.asc().nullsfirst())
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    logger.info("Stage 2: %d restaurants with place_id to (re-)enrich", len(rows))

    if not commit:
        return 0

    enriched = 0
    for r in rows:
        ok = await refresh_restaurant_from_google(db, r.id, force=True)
        marker = "✓" if ok else "✗"
        logger.info("  %s %s", marker, r.name)
        if ok:
            enriched += 1
        await asyncio.sleep(0.2)
    return enriched


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="Apply changes (real API calls + DB writes)")
    parser.add_argument("--skip-find-place", action="store_true", help="Skip Stage 1")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip Stage 2")
    parser.add_argument("--limit", type=int, default=None, help="Limit per stage")
    args = parser.parse_args()

    if args.commit and not settings.GOOGLE_PLACES_API_KEY:
        logger.error("GOOGLE_PLACES_API_KEY is not set; aborting.")
        sys.exit(2)

    if not args.commit:
        logger.info("DRY-RUN — pass --commit to apply changes.")

    async with async_session() as db:
        if not args.skip_find_place:
            resolved = await stage_find_place_ids(db, commit=args.commit, limit=args.limit)
            logger.info("Stage 1 done: place_ids resolved = %d", resolved)
        if not args.skip_enrich:
            enriched = await stage_enrich(db, commit=args.commit, limit=args.limit)
            logger.info("Stage 2 done: enriched = %d", enriched)


if __name__ == "__main__":
    asyncio.run(main())
