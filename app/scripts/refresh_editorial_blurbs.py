"""One-shot backfill: regenerate dish editorial blurbs.

Cuándo correrlo:
- Después de un cambio del prompt o del shape de salida del enricher
  (bumpear `EDITORIAL_PROMPT_VERSION` y correr este script).
- Después de migrar a un modelo distinto y querer re-validar los textos.

Modos:

    python -m app.scripts.refresh_editorial_blurbs
        Regenera solo los platos cuyo `editorial_prompt_version` no matchea
        la versión actual o que no tienen blurb. Idempotente: re-correr es
        seguro y barato si todo ya está al día.

    python -m app.scripts.refresh_editorial_blurbs --all
        Fuerza la regeneración de todos los platos. Útil después de bumpear
        el prompt — limpia la cache compartida primero para que los textos
        viejos no contaminen los nuevos lookups.

La cache compartida (`dish_editorial_cache`) hace que el costo escale con
el número de **platos distintos** (asado, milanesa, sushi, ...), no con el
número de filas en `dishes`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import delete, or_, select

from app.database import async_session
from app.models.dish import Dish, DishEditorialCache
from app.services.dish_editorial_enricher import (
    EDITORIAL_PROMPT_VERSION,
    _api_key,
    refresh_dish_blurb,
)


CONCURRENCY = 5  # Limita llamadas paralelas a Anthropic.

logger = logging.getLogger("refresh_editorial_blurbs")


async def _backfill(force_all: bool) -> tuple[int, int]:
    """Devuelve (procesados, actualizados)."""
    async with async_session() as db:
        if force_all:
            # Limpiamos la cache para que el primer plato de cada (name, cuisine)
            # vuelva a llamar al LLM y rehidrate cache fresca.
            await db.execute(delete(DishEditorialCache))
            await db.commit()
            stmt = select(Dish.id).order_by(Dish.created_at)
        else:
            stmt = (
                select(Dish.id)
                .where(
                    or_(
                        Dish.editorial_prompt_version != EDITORIAL_PROMPT_VERSION,
                        Dish.editorial_prompt_version.is_(None),
                        Dish.editorial_blurb.is_(None),
                    )
                )
                .order_by(Dish.created_at)
            )
        dish_ids = list((await db.execute(stmt)).scalars().all())

    if not dish_ids:
        logger.info("Nada para regenerar — todos los blurbs están al día.")
        return 0, 0

    logger.info("Regenerando %d platos (concurrencia=%d)", len(dish_ids), CONCURRENCY)
    sem = asyncio.Semaphore(CONCURRENCY)
    written = 0
    processed = 0

    async def _one(dish_id) -> bool:
        async with sem:
            # Sesión propia por dish: cada `refresh_dish_blurb` commitea sola.
            # Aísla failures para no rollback-ear todo el batch.
            async with async_session() as session:
                try:
                    return await refresh_dish_blurb(session, dish_id, force=force_all)
                except Exception:
                    logger.exception("Fallo regenerando dish_id=%s", dish_id)
                    return False

    BATCH = 25
    for i in range(0, len(dish_ids), BATCH):
        chunk = dish_ids[i : i + BATCH]
        results = await asyncio.gather(*(_one(d) for d in chunk))
        for ok in results:
            processed += 1
            if ok:
                written += 1
        logger.info(
            "  %d/%d procesados (actualizados=%d)",
            min(i + BATCH, len(dish_ids)),
            len(dish_ids),
            written,
        )

    return processed, written


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Fuerza la regeneración de todos los platos y limpia la cache "
            "compartida. Sin esta flag, solo se regeneran los blurbs stale."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    if not _api_key():
        logger.error(
            "Sin API key (ANTHROPIC_API_KEY / EDITORIAL_API_KEY / CHAT_API_KEY). "
            "El backfill no puede correr."
        )
        sys.exit(2)

    processed, written = await _backfill(force_all=args.all)
    logger.info(
        "Done. prompt_version=%s processed=%d written=%d",
        EDITORIAL_PROMPT_VERSION,
        processed,
        written,
    )


if __name__ == "__main__":
    asyncio.run(main())
