"""Auto-promote la mejor foto de review como cover oficial de cada plato
en restaurants SIN owner verificado.

Política deliberada — set-once:
    Solo asigna cover cuando ``dishes.cover_image_url IS NULL``. Nunca
    reemplaza un cover existente, así el card del plato no "baila" cuando
    aparece una review marginalmente mejor. Si el restaurant después se
    claimea, el owner puede pisar este valor desde el dashboard.

Restaurants CON owner verificado quedan fuera del scope: ese caso lo
maneja el owner desde la UI (subir nueva o elegir de reviews).

Score por candidato (mayor = mejor):
    rating
        - 0.01 × (días transcurridos desde la review)

Filtro de calidad mínimo: ``rating >= 3.0`` — si nadie le puso al menos
un 3, el plato sigue mostrando el fallback genérico.

Empate: rating desc → created_at desc → image.id (orden estable).

Uso:
    python scripts/promote_dish_covers.py            # dry-run (default)
    python scripts/promote_dish_covers.py --commit   # escribe

Pensado para correr desde Railway Cron (1 vez al día). Es idempotente:
una segunda corrida en el mismo día devuelve 0 cambios.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


logger = logging.getLogger("promote_dish_covers")


# Min rating de la review para que su foto sea elegible. Por debajo de 3 las
# fotos suelen acompañar quejas (plato mal presentado, frío, mal montaje) —
# no querés eso como hero del plato en el menú.
MIN_RATING = 3.0
# Penalización por antigüedad: 0.01 puntos por día. A 100 días pierde 1 punto.
# Una foto de hace 1 año empata con una nueva 1 estrella menor — razonable.
FRESHNESS_DECAY = 0.01


PREVIEW_SQL = text(
    """
    WITH ranked AS (
        SELECT
            dr.dish_id,
            d.name              AS dish_name,
            r.name              AS restaurant_name,
            dri.url             AS image_url,
            dri.id              AS image_id,
            dr.id               AS review_id,
            dr.rating::float    AS rating,
            dr.created_at       AS review_created_at,
            dr.rating::float - :decay
                * EXTRACT(EPOCH FROM (now() - dr.created_at)) / 86400.0
                                AS score,
            ROW_NUMBER() OVER (
                PARTITION BY dr.dish_id
                ORDER BY
                    dr.rating::float
                        - :decay * EXTRACT(EPOCH FROM (now() - dr.created_at)) / 86400.0
                        DESC,
                    dr.created_at DESC,
                    dri.id ASC
            )                   AS rn
        FROM dish_reviews dr
        JOIN dish_review_images dri ON dri.dish_review_id = dr.id
        JOIN dishes d ON d.id = dr.dish_id
        JOIN restaurants r ON r.id = d.restaurant_id
        WHERE
            d.cover_image_url IS NULL
            AND r.claimed_by_user_id IS NULL
            AND dr.rating >= :min_rating
            AND dri.display_order = 0
    )
    SELECT
        dish_id, dish_name, restaurant_name,
        image_url, image_id, review_id, rating, review_created_at, score
    FROM ranked
    WHERE rn = 1
    ORDER BY score DESC;
    """
)


COMMIT_SQL = text(
    """
    WITH ranked AS (
        SELECT
            dr.dish_id,
            dri.url AS image_url,
            ROW_NUMBER() OVER (
                PARTITION BY dr.dish_id
                ORDER BY
                    dr.rating::float
                        - :decay * EXTRACT(EPOCH FROM (now() - dr.created_at)) / 86400.0
                        DESC,
                    dr.created_at DESC,
                    dri.id ASC
            ) AS rn
        FROM dish_reviews dr
        JOIN dish_review_images dri ON dri.dish_review_id = dr.id
        JOIN dishes d ON d.id = dr.dish_id
        JOIN restaurants r ON r.id = d.restaurant_id
        WHERE
            d.cover_image_url IS NULL
            AND r.claimed_by_user_id IS NULL
            AND dr.rating >= :min_rating
            AND dri.display_order = 0
    ),
    picks AS (
        SELECT dish_id, image_url FROM ranked WHERE rn = 1
    )
    UPDATE dishes
    SET cover_image_url = picks.image_url
    FROM picks
    WHERE dishes.id = picks.dish_id
        AND dishes.cover_image_url IS NULL
    RETURNING dishes.id, dishes.name, dishes.cover_image_url;
    """
)


async def run(session: AsyncSession, *, commit: bool, limit_preview: int) -> int:
    rows = (
        await session.execute(
            PREVIEW_SQL, {"min_rating": MIN_RATING, "decay": FRESHNESS_DECAY}
        )
    ).all()

    if not rows:
        print("No dishes to promote — every unclaimed restaurant either already "
              "has covers, or has no review photos meeting the quality threshold.")
        return 0

    print(f"\nFound {len(rows)} dish(es) eligible for cover auto-promotion.")
    print(f"  min_rating={MIN_RATING}  freshness_decay={FRESHNESS_DECAY}/day\n")

    preview = rows[:limit_preview]
    for row in preview:
        print(
            f"  • [{row.score:5.2f}] {row.restaurant_name} → {row.dish_name} "
            f"(rating={row.rating:.1f}, review {row.review_id})"
        )
    if len(rows) > limit_preview:
        print(f"  … and {len(rows) - limit_preview} more.")

    if not commit:
        print("\nDry-run — pass --commit to write these changes.")
        return 0

    result = await session.execute(
        COMMIT_SQL, {"min_rating": MIN_RATING, "decay": FRESHNESS_DECAY}
    )
    updated = result.all()
    await session.commit()
    print(f"\nUpdated {len(updated)} dish(es).")
    return len(updated)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit", action="store_true",
        help="Apply the writes. Without this flag the script just prints "
             "what it would do.",
    )
    parser.add_argument(
        "--limit-preview", type=int, default=20,
        help="Cap on rows printed in the preview (default 20). Doesn't affect "
             "what gets written when --commit is passed.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with Session() as session:
            await run(session, commit=args.commit, limit_preview=args.limit_preview)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
