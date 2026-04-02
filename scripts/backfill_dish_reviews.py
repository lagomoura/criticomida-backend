"""
Backfill DishReviews for imported dishes using the parent restaurant's data.

For each dish without a review:
  - rating    = restaurant's food_quality dimension score (rounded to int)
  - note      = restaurant's VisitDiaryEntry text (most recent), or fallback
  - date      = diary entry visit_date, or dish created_at date

Run from backend/:
    python scripts/backfill_dish_reviews.py \\
        --user-email admin@criticomida.com \\
        --db-url "postgresql+asyncpg://user:pass@localhost:5433/criticomida"
    # add --commit to write
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.dish import Dish, DishReview
from app.models.restaurant import RatingDimension, Restaurant, RestaurantRatingDimension, VisitDiaryEntry
from app.models.user import User
from app.services.rating_service import update_dish_rating, update_restaurant_rating


async def backfill(session: AsyncSession, user: User, dry_run: bool) -> None:
    # Load all dishes created by this user
    dishes_result = await session.execute(
        select(Dish).where(Dish.created_by == user.id)
    )
    dishes = list(dishes_result.scalars().all())
    print(f"Found {len(dishes)} dishes created by {user.email}\n")

    created = already_exists = no_rating = 0

    for dish in dishes:
        # Skip if review already exists
        existing = await session.execute(
            select(DishReview).where(
                DishReview.dish_id == dish.id,
                DishReview.user_id == user.id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            already_exists += 1
            continue

        # Get restaurant's food_quality dimension score
        dim_result = await session.execute(
            select(RestaurantRatingDimension).where(
                RestaurantRatingDimension.restaurant_id == dish.restaurant_id,
                RestaurantRatingDimension.user_id == user.id,
                RestaurantRatingDimension.dimension == RatingDimension.food_quality,
            )
        )
        food_quality = dim_result.scalar_one_or_none()

        if food_quality is None:
            # Fall back to any dimension rating we have
            any_dim = await session.execute(
                select(RestaurantRatingDimension).where(
                    RestaurantRatingDimension.restaurant_id == dish.restaurant_id,
                    RestaurantRatingDimension.user_id == user.id,
                ).limit(1)
            )
            food_quality = any_dim.scalar_one_or_none()

        if food_quality is None:
            print(f"  [SKIP] {dish.name!r} — no rating on restaurant")
            no_rating += 1
            continue

        rating = max(1, min(5, round(float(food_quality.score))))

        # Get most recent diary entry for this restaurant
        diary_result = await session.execute(
            select(VisitDiaryEntry)
            .where(
                VisitDiaryEntry.restaurant_id == dish.restaurant_id,
                VisitDiaryEntry.created_by == user.id,
            )
            .order_by(VisitDiaryEntry.visit_date.desc())
            .limit(1)
        )
        diary = diary_result.scalar_one_or_none()

        note = diary.diary_text if diary else "Valoración importada desde reseña de Google Maps."
        date_tasted = diary.visit_date if diary else dish.created_at.date()

        # Get restaurant name for display
        rest_result = await session.execute(
            select(Restaurant.name).where(Restaurant.id == dish.restaurant_id)
        )
        rest_name = rest_result.scalar_one_or_none() or "?"

        print(f"{'[DRY-RUN] ' if dry_run else ''}→ {dish.name!r}  ({rest_name})")
        print(f"  rating={rating}  date={date_tasted}  note={note[:60]!r}{'...' if len(note) > 60 else ''}")

        if not dry_run:
            review = DishReview(
                dish_id=dish.id,
                user_id=user.id,
                date_tasted=date_tasted,
                note=note,
                rating=rating,
            )
            session.add(review)
            await session.flush()
            await update_dish_rating(session, dish.id)
            await update_restaurant_rating(session, dish.restaurant_id)

        created += 1

    print(f"\n{'=' * 50}")
    mode = "DRY-RUN" if dry_run else "IMPORT"
    print(f"=== {mode} SUMMARY ===")
    label = "Would create" if dry_run else "Created"
    print(f"  {label:<14}: {created} dish reviews")
    print(f"  Already exist  : {already_exists}")
    print(f"  No rating data : {no_rating}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-email", required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--db-url", default=None)
    args = parser.parse_args()

    db_url = args.db_url or settings.DATABASE_URL
    engine = create_async_engine(db_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        user_row = await session.execute(select(User).where(User.email == args.user_email))
        user = user_row.scalar_one_or_none()
        if user is None:
            print(f"ERROR: User not found: {args.user_email!r}")
            sys.exit(1)

        dry_run = not args.commit
        if dry_run:
            print("*** DRY-RUN — pass --commit to write ***\n")

        try:
            await backfill(session, user, dry_run)
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
