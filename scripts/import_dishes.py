"""
Import dishes from dish_mapping.yaml into CritiComida database.

Creates a Dish for each entry, and optionally a DishReview if rating/note
are provided. Run from backend/:

    # Dry-run (default):
    python scripts/import_dishes.py \\
        --user-email admin@criticomida.com \\
        --db-url "postgresql+asyncpg://user:pass@localhost:5433/criticomida"

    # Commit:
    python scripts/import_dishes.py ... --commit
"""

import argparse
import asyncio
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# PyYAML — install with: pip install pyyaml
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml")
    sys.exit(1)

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.dish import Dish, DishReview
from app.models.restaurant import Restaurant
from app.models.user import User
from app.services.rating_service import update_dish_rating, update_restaurant_rating

MAPPING_PATH = Path("../google_maps_data/dish_mapping.yaml")


def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


async def find_restaurant_by_name(session: AsyncSession, name: str) -> Restaurant | None:
    """Fuzzy match: exact name first, then case-insensitive contains."""
    result = await session.execute(
        select(Restaurant).where(Restaurant.name == name)
    )
    restaurant = result.scalar_one_or_none()
    if restaurant:
        return restaurant

    # Case-insensitive fallback
    result = await session.execute(select(Restaurant))
    all_restaurants = list(result.scalars().all())
    name_lower = name.lower()
    for r in all_restaurants:
        if r.name.lower() == name_lower:
            return r
    # Partial match as last resort
    for r in all_restaurants:
        if name_lower in r.name.lower() or r.name.lower() in name_lower:
            return r
    return None


async def import_dishes(
    entries: list[dict],
    session: AsyncSession,
    user: User,
    dry_run: bool,
) -> None:
    imported = skipped_manual = skipped_no_restaurant = errors = 0

    for entry in entries:
        restaurant_name = str(entry.get("restaurant", "")).strip()
        dish_name = str(entry.get("dish", "")).strip()
        note = str(entry.get("note", "") or "").strip()
        rating = entry.get("rating")
        photo_url = str(entry.get("photo_url", "") or "").strip() or None
        dish_date_str = str(entry.get("date", ""))

        # Parse date
        try:
            dish_date = datetime.fromisoformat(dish_date_str).date()
        except Exception:
            dish_date = datetime.now(timezone.utc).date()

        # Skip entries marked as SKIP or ???
        if restaurant_name in ("SKIP", "???", "", "[not in DB]") or restaurant_name.startswith("[not in DB"):
            print(f"  [SKIP] {dish_name!r} — restaurant not resolved")
            skipped_manual += 1
            continue

        has_review = bool(note) or (rating is not None)

        print(f"\n{'[DRY-RUN] ' if dry_run else ''}→ {dish_name!r}")
        print(f"  Restaurant : {restaurant_name}")
        print(f"  Date       : {dish_date}  |  photo={bool(photo_url)}  |  review={has_review}")

        if dry_run:
            imported += 1
            continue

        try:
            restaurant = await find_restaurant_by_name(session, restaurant_name)
            if restaurant is None:
                print(f"  [ERROR] Restaurant not found: {restaurant_name!r}")
                skipped_no_restaurant += 1
                continue

            # Check if dish already exists
            dup = await session.execute(
                select(Dish).where(
                    Dish.restaurant_id == restaurant.id,
                    Dish.name == dish_name,
                )
            )
            existing_dish = dup.scalar_one_or_none()

            if existing_dish is None:
                dish = Dish(
                    restaurant_id=restaurant.id,
                    name=dish_name,
                    cover_image_url=photo_url,
                    created_by=user.id,
                )
                session.add(dish)
                await session.flush()
                print(f"  [OK] Dish created (id={dish.id})")
            else:
                dish = existing_dish
                print(f"  [EXISTS] Dish already exists (id={dish.id})")

            # Create review if note or rating provided
            if has_review:
                dup_review = await session.execute(
                    select(DishReview).where(
                        DishReview.dish_id == dish.id,
                        DishReview.user_id == user.id,
                    )
                )
                if dup_review.scalar_one_or_none() is not None:
                    print(f"  [EXISTS] Review already exists for this dish")
                else:
                    review_note = note or f"Plato fotografiado en {dish_date}"
                    review_rating = int(rating) if rating is not None else 3
                    review = DishReview(
                        dish_id=dish.id,
                        user_id=user.id,
                        date_tasted=dish_date,
                        note=review_note,
                        rating=review_rating,
                    )
                    session.add(review)
                    await session.flush()

                    await update_dish_rating(session, dish.id)
                    await update_restaurant_rating(session, restaurant.id)
                    print(f"  [OK] Review created (rating={review_rating})")

            imported += 1

        except Exception as exc:
            print(f"  [ERROR] {exc}")
            errors += 1

    print(f"\n{'=' * 50}")
    mode = "DRY-RUN" if dry_run else "IMPORT"
    print(f"=== {mode} SUMMARY ===")
    label = "Would import" if dry_run else "Imported"
    print(f"  {label:<14}: {imported}")
    print(f"  Skipped (???) : {skipped_manual}")
    if not dry_run:
        print(f"  No restaurant : {skipped_no_restaurant}")
        print(f"  Errors        : {errors}")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import dishes from dish_mapping.yaml"
    )
    parser.add_argument(
        "--mapping", default=str(MAPPING_PATH),
        help="Path to dish_mapping.yaml",
    )
    parser.add_argument(
        "--user-email", required=True,
        help="Email of the user to attribute dishes to",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Write to DB (default is dry-run)",
    )
    parser.add_argument("--db-url", default=None)
    args = parser.parse_args()

    mapping_path = Path(args.mapping)
    if not mapping_path.exists():
        print(f"ERROR: Mapping file not found: {mapping_path}")
        print("Run generate_dish_mapping.py first.")
        sys.exit(1)

    with open(mapping_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    entries = data.get("dishes", [])
    print(f"Loaded {len(entries)} dishes from {mapping_path}")

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
        print(f"User: {user.display_name} ({user.email})")

        dry_run = not args.commit
        if dry_run:
            print("\n*** DRY-RUN — pass --commit to write to DB ***\n")

        try:
            await import_dishes(entries, session, user, dry_run)
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
