"""
Import Google Maps Takeout reviews into CritiComida database.

Filters to gastronomic places only and auto-assigns categories,
creating new ones when no existing category matches.

Usage (from backend/ directory):
    # Dry-run (default):
    python scripts/import_google_maps.py \\
        --input "../google_maps_data/Takeout/Maps (Seus lugares)/Comentários.json" \\
        --user-email admin@criticomida.com \\
        --db-url "postgresql+asyncpg://user:pass@localhost:5433/criticomida"

    # Commit to DB:
    python scripts/import_google_maps.py ... --commit
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.restaurant import (
    RatingDimension,
    Restaurant,
    RestaurantRatingDimension,
    VisitDiaryEntry,
)
from app.models.user import User


# ---------------------------------------------------------------------------
# Classification: non-gastronomic exclusion
# ---------------------------------------------------------------------------

# If any of these regex patterns match the place name → NOT food → skip.
# Patterns are matched case-insensitively against the full name.
NON_FOOD_PATTERNS: list[str] = [
    # Health / professional services
    r"psicólog", r"psicolog", r"veterinaria", r"clínica", r"clinica",
    r"\bdoctor\b", r"\bmédico\b", r"\bmedico\b", r"farmacia",
    # Automotive
    r"lubricentro", r"taller mecánico", r"gomería",
    # Entertainment (non-food)
    r"cinépolis", r"cinepolis", r"\bcine\b", r"trampoline", r"jump park",
    r"mr\.?\s*fly\b", r"aerosillas",
    r"\bslots\b", r"avellaneda slots", r"casino\b",
    # Retail / services
    r"correo argentino", r"western union", r"\bbanco\b",
    r"glam hair", r"tattoo", r"tatuaje",
    r"\bgym\b", r"gimnasio", r"sitrin gym",
    # Malls / shopping centers
    r"alto avellaneda shopping",
    r"centro comercial parque avellaneda",
    r"abasto\b",                 # Abasto shopping mall
    # Parks / plazas / outdoor spaces
    r"micaela bastidas park",
    r"parque lezama",
    r"praça de maio", r"plaza de mayo",
    r"praça imigrantes",
    # Cultural / institutional
    r"museu\b", r"museo\b",
    r"centro cultural recoleta",
    r"complejo cerro",           # mountain/ski complex
    # Transport
    r"terminal.*buquebus", r"buquebus.*terminal",
    # Misc clearly non-food
    r"andrea migliano",          # person name (dentist/professional)
]

_NON_FOOD_RE = re.compile(
    "|".join(NON_FOOD_PATTERNS), re.IGNORECASE
)


def is_gastronomic(name: str) -> bool:
    """Return True if the place name does NOT match any non-food pattern."""
    return not bool(_NON_FOOD_RE.search(name))


# Category logic removed: a restaurant's categories are determined by its
# dishes. Restaurants are imported without a category_id (nullable).


# ---------------------------------------------------------------------------
# Google question → RatingDimension mapping
# ---------------------------------------------------------------------------

QUESTION_TO_DIMENSION: dict[str, RatingDimension] = {
    "Comida": RatingDimension.food_quality,
    "Serviço": RatingDimension.service,
    "Ambiente": RatingDimension.ambiance,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def parse_review_date(date_str: str) -> date:
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def extract_dimensions(questions: list[dict]) -> dict[RatingDimension, Decimal]:
    dims: dict[RatingDimension, Decimal] = {}
    for q in questions:
        label = q.get("question", "")
        rating = q.get("rating")
        if label in QUESTION_TO_DIMENSION and isinstance(rating, (int, float)):
            dims[QUESTION_TO_DIMENSION[label]] = Decimal(str(rating))
    return dims


def load_features(path: Path, min_rating: int) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [
        feat for feat in data.get("features", [])
        if "location" in feat.get("properties", {})
        and feat["properties"].get("five_star_rating_published", 0) >= min_rating
    ]


# ---------------------------------------------------------------------------
# Core import
# ---------------------------------------------------------------------------

async def import_reviews(
    features: list[dict],
    session: AsyncSession,
    user: User,
    dry_run: bool,
) -> None:
    total = len(features)
    created = skipped_non_food = skipped_exists = errors = 0

    for feat in features:
        props = feat["properties"]
        geom = feat.get("geometry", {})
        loc = props["location"]

        name = loc.get("name", "").strip()
        address = loc.get("address", "").strip()
        star_rating = props.get("five_star_rating_published", 0)
        review_text = (props.get("review_text_published") or "").strip()
        questions = props.get("questions", [])
        review_date = parse_review_date(props.get("date", ""))

        coords = geom.get("coordinates", [])
        longitude = Decimal(str(coords[0])) if len(coords) > 0 else None
        latitude = Decimal(str(coords[1])) if len(coords) > 1 else None

        # ── 1. Gastronomic filter ──────────────────────────────────────────
        if not is_gastronomic(name):
            print(f"  [SKIP non-food] {name}")
            skipped_non_food += 1
            continue

        dimensions = extract_dimensions(questions)
        if not dimensions and star_rating > 0:
            dimensions[RatingDimension.food_quality] = Decimal(str(star_rating))

        print(f"\n{'[DRY-RUN] ' if dry_run else ''}→ {name}")
        print(f"  Address  : {address}")
        print(f"  Coords   : lat={latitude}, lng={longitude}")
        print(f"  Rating   : {star_rating}★  dims={len(dimensions)}  text={bool(review_text)}")

        if dry_run:
            created += 1
            continue

        try:
            # ── 3. Skip duplicates ─────────────────────────────────────────
            dup = await session.execute(
                select(Restaurant).where(
                    Restaurant.name == name,
                    Restaurant.location_name == address,
                )
            )
            if dup.scalar_one_or_none() is not None:
                print(f"  [SKIP duplicate]")
                skipped_exists += 1
                continue

            # ── 4. Create restaurant ───────────────────────────────────────
            slug = slugify(name)
            taken = await session.execute(
                select(Restaurant.id).where(Restaurant.slug == slug).limit(1)
            )
            if taken.scalar_one_or_none() is not None:
                import uuid
                slug = f"{slug}-{uuid.uuid4().hex[:8]}"

            restaurant = Restaurant(
                slug=slug,
                name=name,
                location_name=address,
                latitude=latitude,
                longitude=longitude,
                created_by=user.id,
            )
            session.add(restaurant)
            await session.flush()

            # ── 6. Dimension ratings ───────────────────────────────────────
            if dimensions:
                table = RestaurantRatingDimension.__table__
                for dim, score in dimensions.items():
                    stmt = pg_insert(table).values(
                        restaurant_id=restaurant.id,
                        user_id=user.id,
                        dimension=dim,
                        score=score,
                    ).on_conflict_do_update(
                        constraint="uq_rest_user_dimension",
                        set_={"score": score},
                    )
                    await session.execute(stmt)

            # ── 7. Diary entry ─────────────────────────────────────────────
            if review_text:
                session.add(VisitDiaryEntry(
                    restaurant_id=restaurant.id,
                    visit_date=review_date,
                    diary_text=review_text,
                    created_by=user.id,
                ))

            await session.flush()
            print(f"  [OK] slug={slug!r}")
            created += 1

        except Exception as exc:
            print(f"  [ERROR] {exc}")
            errors += 1

    print(f"\n{'=' * 50}")
    mode = "DRY-RUN" if dry_run else "IMPORT"
    print(f"=== {mode} SUMMARY  ({total} candidates) ===")
    label = "Would import" if dry_run else "Imported"
    print(f"  {label:<14}: {created}")
    print(f"  Non-food skipped: {skipped_non_food}")
    if not dry_run:
        print(f"  Duplicates skipped: {skipped_exists}")
        print(f"  Errors          : {errors}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Google Maps Takeout reviews into CritiComida"
    )
    parser.add_argument(
        "--input",
        default='../google_maps_data/Takeout/Maps (Seus lugares)/Comentários.json',
        help="Path to Comentários.json",
    )
    parser.add_argument(
        "--user-email", required=True,
        help="Email of the user to attribute imports to",
    )
    parser.add_argument(
        "--min-rating", type=int, default=1,
        help="Minimum star rating to import (default 1 = all rated)",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Write to DB (default is dry-run)",
    )
    parser.add_argument(
        "--db-url", default=None,
        help="Override DATABASE_URL (e.g. postgresql+asyncpg://...@localhost:5433/db)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}")
        sys.exit(1)

    features = load_features(input_path, args.min_rating)
    print(f"Loaded {len(features)} rated features (min_rating={args.min_rating})")

    db_url = args.db_url or settings.DATABASE_URL
    engine = create_async_engine(db_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        user_row = await session.execute(
            select(User).where(User.email == args.user_email)
        )
        user = user_row.scalar_one_or_none()
        if user is None:
            print(f"ERROR: User not found: {args.user_email!r}")
            sys.exit(1)
        print(f"User : {user.display_name} ({user.email}) role={user.role.value}")

        dry_run = not args.commit
        if dry_run:
            print("\n*** DRY-RUN — pass --commit to write to DB ***\n")

        try:
            await import_reviews(features, session, user, dry_run)
            if not dry_run:
                await session.commit()
                print("\nCommitted successfully.")
        except Exception:
            await session.rollback()
            raise
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
