"""
Assign category_id to restaurants that currently have none.

Uses name-based keyword rules to match each restaurant to the best category.
Restaurants that don't match any rule are left as NULL and listed at the end.

Run from backend/:
    python scripts/assign_restaurant_categories.py \\
        --db-url "postgresql+asyncpg://user:pass@localhost:5433/criticomida"
    # add --commit to write
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.category import Category
from app.models.restaurant import Restaurant

# ── Rules: (keywords_in_name, category_slug) ────────────────────────────────
# Each rule is a list of lowercase substrings — ALL must appear in the name
# for the rule to match (use single-item lists for OR-style matching).
# Rules are evaluated in order; first match wins.

RULES: list[tuple[list[str], str]] = [
    # ── Helados ──────────────────────────────────────────────────────────────
    (["heladería"],             "helados"),
    (["helado"],                "helados"),

    # ── Parrilla ─────────────────────────────────────────────────────────────
    (["parrilla"],              "parrillas"),
    (["grill"],                 "parrillas"),
    (["estancia"],              "parrillas"),
    (["rancho"],                "parrillas"),
    (["la rural"],              "parrillas"),
    (["la campiña"],            "parrillas"),
    (["patio de los lecheros"], "parrillas"),

    # ── Hamburguesas ─────────────────────────────────────────────────────────
    (["burger"],                "burguers"),
    (["burguer"],               "burguers"),
    (["mcdonald"],              "burguers"),
    (["mostaza"],               "burguers"),

    # ── Japonesa ─────────────────────────────────────────────────────────────
    (["ramen"],                 "japan-food"),
    (["sushi"],                 "japan-food"),
    (["poke"],                  "japan-food"),
    (["bao kitchen"],           "japan-food"),

    # ── China ────────────────────────────────────────────────────────────────
    (["dumpling"],              "chinafood"),
    (["koi"],                   "chinafood"),
    (["rong cheng"],            "chinafood"),
    (["gāo"],                   "chinafood"),
    (["cang tin"],              "chinafood"),

    # ── Tailandesa ───────────────────────────────────────────────────────────
    (["thai"],                  "thaifood"),
    (["khaosan"],               "thaifood"),

    # ── Coreana ──────────────────────────────────────────────────────────────
    (["bbq"],                   "koreanfood"),
    (["mocozi"],                "koreanfood"),
    (["k-bbq"],                 "koreanfood"),

    # ── Mexicana ─────────────────────────────────────────────────────────────
    (["taco"],                  "mexico-food"),
    (["mexican"],               "mexico-food"),
    (["mexicana"],              "mexico-food"),
    (["chichilo"],              "mexico-food"),

    # ── Israelí ──────────────────────────────────────────────────────────────
    (["israeli"],               "israelfood"),
    (["israel"],                "israelfood"),
    (["eretz"],                 "israelfood"),

    # ── Árabe ────────────────────────────────────────────────────────────────
    (["shawarma"],              "arabic-food"),
    (["kebab"],                 "arabic-food"),
    (["shami"],                 "arabic-food"),

    # ── Dulces / Pastelería ───────────────────────────────────────────────────
    (["panadería"],             "dulces"),
    (["obrador"],               "dulces"),
    (["tartas"],                "dulces"),
    (["tarta"],                 "dulces"),
    (["delicias"],              "dulces"),
    (["pastel"],                "dulces"),
    (["repostería"],            "dulces"),

    # ── Desayunos / Brunch ────────────────────────────────────────────────────
    (["starbucks"],             "desayunos"),
    (["nucha"],                 "desayunos"),
    (["panera"],                "desayunos"),
    (["coffee"],                "desayunos"),
    (["café"],                  "desayunos"),
    (["cafe"],                  "desayunos"),
    (["brunch"],                "brunchs"),
    (["1870 beer"],             "brunchs"),
]


def match_category(name: str, slug_to_id: dict[str, int]) -> tuple[int | None, str | None]:
    """Return (category_id, slug) for the first matching rule, or (None, None)."""
    lower = name.lower()
    for keywords, slug in RULES:
        if all(kw in lower for kw in keywords):
            cat_id = slug_to_id.get(slug)
            if cat_id:
                return cat_id, slug
    return None, None


async def assign(session: AsyncSession, dry_run: bool) -> None:
    # Load categories
    cats = list((await session.execute(select(Category))).scalars().all())
    slug_to_id = {c.slug: c.id for c in cats}

    # Load uncategorized restaurants
    result = await session.execute(
        select(Restaurant).where(Restaurant.category_id.is_(None))
    )
    restaurants = list(result.scalars().all())
    print(f"Found {len(restaurants)} restaurants without a category\n")

    assigned = skipped = 0

    for r in restaurants:
        cat_id, slug = match_category(r.name, slug_to_id)
        if cat_id is None:
            print(f"  [--] {r.name!r}")
            skipped += 1
            continue

        print(f"  {'[DRY] ' if dry_run else ''}→ {r.name!r}  →  {slug}")
        if not dry_run:
            await session.execute(
                update(Restaurant)
                .where(Restaurant.id == r.id)
                .values(category_id=cat_id)
            )
        assigned += 1

    print(f"\n{'=' * 50}")
    mode = "DRY-RUN" if dry_run else "DONE"
    print(f"=== {mode} ===")
    label = "Would assign" if dry_run else "Assigned"
    print(f"  {label:<14}: {assigned}")
    print(f"  No match       : {skipped}  (left as NULL)")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--db-url", default=None)
    args = parser.parse_args()

    db_url = args.db_url or settings.DATABASE_URL
    engine = create_async_engine(db_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        dry_run = not args.commit
        if dry_run:
            print("*** DRY-RUN — pass --commit to write ***\n")
        try:
            await assign(session, dry_run)
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
