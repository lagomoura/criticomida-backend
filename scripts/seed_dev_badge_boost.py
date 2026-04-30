#!/usr/bin/env python3
"""Empuja dishes específicos arriba del umbral de Chef Badge / Gem Badge para
que el mapa muestre los acentos visuales sin tener que sembrar tropecientas
reviews.

El umbral en discovery_service.py es:
    BADGE_AVG_THRESHOLD     = 2.7  (sobre 3)
    MIN_REVIEWS_FOR_BADGE   = 3

Estrategia: para 2 dishes que ya tienen 1 review con execution=3 (de
seed_dev_critics.py), se agregan 2 reviews extra desde otros críticos
con execution=3 → suma 3 reviews exec=3 → CHEF. Lo mismo para 2 dishes
con value_prop=3 → GEM. Total: 8 reviews extra, 4 restaurantes con
badges visibles en el mapa.

Idempotente: si ya hay review de ese (dish, user), no la duplica.

Usage
-----
    docker exec -e DATABASE_URL='postgresql+asyncpg://criticomida:criticomida_secret@db:5432/criticomida' \\
        backend-api-1 python scripts/seed_dev_badge_boost.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

import asyncpg

# (dish_name, restaurant_name, target_pillar, value_3=True)
# Seguimos el patrón de seed_dev_critics: un crítico ya tiene una review en
# cada plato. Acá agregamos 2 más desde críticos distintos.
CHEF_BOOSTS: list[tuple[str, str]] = [
    ("Açai", "HANA Poke & Bar"),
    ("Burrito", "La Fábrica del Taco Villa Urquiza"),
]

GEM_BOOSTS: list[tuple[str, str]] = [
    ("Beer", "Cervecería Untertürkheim"),
    ("Café Turco", "Eretz Cantina Israeli"),
]

NOTES_CHEF: list[str] = [
    "La cocina se nota con muñeca. Punto de cocción exacto.",
    "Top en su categoría. Vuelvo seguro.",
    "Cada bocado deja claro que el chef sabe lo que hace.",
    "Para mí, mejor ejecución de la zona.",
]

NOTES_GEM: list[str] = [
    "Por lo que pagás, te llevás el doble. Imbatible.",
    "Salís pensando que pagaste poco para todo lo que recibís.",
    "Relación precio/calidad de las mejores que probé.",
    "Hallazgo. Vale cada peso.",
]


def normalize_dsn(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://"):]
    return url


async def boost(
    conn: asyncpg.Connection,
    dish_name: str,
    restaurant_name: str,
    pillar: str,  # 'execution' or 'value_prop'
    extra_critics: list[str],
    notes: list[str],
    minutes_back_start: int,
) -> int:
    """Inserta reviews adicionales con pillar=3 para alcanzar el umbral del badge.

    Devuelve cantidad de reviews insertadas (skip si ya existen)."""
    row = await conn.fetchrow(
        """
        SELECT d.id AS dish_id
        FROM dishes d JOIN restaurants r ON r.id = d.restaurant_id
        WHERE d.name = $1 AND r.name = $2
        """,
        dish_name,
        restaurant_name,
    )
    if row is None:
        print(f"  ⚠ no encontrado: {dish_name} @ {restaurant_name}")
        return 0
    dish_id = row["dish_id"]

    inserted = 0
    for i, critic_email in enumerate(extra_critics):
        critic_id = await conn.fetchval(
            "SELECT id FROM users WHERE email = $1", critic_email
        )
        if critic_id is None:
            print(f"  ⚠ crítico no existe: {critic_email}")
            continue

        pres = 3 if pillar == "presentation" else None
        val = 3 if pillar == "value_prop" else None
        execn = 3 if pillar == "execution" else None
        rating = 4.5 if i == 0 else 5.0
        note = notes[i % len(notes)]
        minutes_back = minutes_back_start + i * 30

        result = await conn.fetchval(
            """
            INSERT INTO dish_reviews
                (id, dish_id, user_id, date_tasted, note, rating,
                 presentation, value_prop, execution, is_anonymous,
                 created_at, updated_at)
            SELECT $1, $2, $3, CURRENT_DATE, $4, $5,
                   $6, $7, $8, false,
                   NOW() - make_interval(mins => $9),
                   NOW() - make_interval(mins => $9)
            WHERE NOT EXISTS (
                SELECT 1 FROM dish_reviews dr
                 WHERE dr.dish_id = $2 AND dr.user_id = $3
            )
            RETURNING id
            """,
            uuid.uuid4(), dish_id, critic_id, note, rating,
            pres, val, execn, minutes_back,
        )
        if result is not None:
            inserted += 1

    badge = "CHEF" if pillar == "execution" else "GEM"
    print(f"  [{badge}] {dish_name} @ {restaurant_name}: +{inserted} reviews")
    return inserted


async def run(dry_run: bool) -> None:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        sys.exit("missing env var: DATABASE_URL")
    dsn = normalize_dsn(raw_url)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        async with conn.transaction():
            total = 0
            # CHEF: cada dish ya tiene 1 review execution=3 de seed_dev_critics.
            # Agregamos 2 más desde críticos distintos.
            for dish_name, rest in CHEF_BOOSTS:
                # Sofía + Tomás boostean Açai (Martín ya tiene la 1ra).
                # Lucía + Tomás boostean Burrito (Sofía ya tiene la 1ra).
                if dish_name == "Açai":
                    extras = ["sofia@example.com", "tomas@example.com"]
                elif dish_name == "Burrito":
                    extras = ["lucia@example.com", "tomas@example.com"]
                else:
                    continue
                total += await boost(
                    conn, dish_name, rest, "execution", extras, NOTES_CHEF, 200
                )

            # GEM: idem, los 2 que ya tienen value_prop=3 de seed_dev_critics.
            for dish_name, rest in GEM_BOOSTS:
                if dish_name == "Beer":
                    extras = ["sofia@example.com", "tomas@example.com"]
                elif dish_name == "Café Turco":
                    extras = ["lucia@example.com", "martin@example.com"]
                else:
                    continue
                total += await boost(
                    conn, dish_name, rest, "value_prop", extras, NOTES_GEM, 220
                )

            # Recalcular agregados de los dishes tocados.
            await conn.execute(
                """
                UPDATE dishes d SET
                  computed_rating = COALESCE((
                    SELECT ROUND(AVG(rating)::numeric, 2)
                    FROM dish_reviews dr WHERE dr.dish_id = d.id
                  ), 0.00),
                  review_count = (
                    SELECT COUNT(*) FROM dish_reviews dr WHERE dr.dish_id = d.id
                  )
                WHERE EXISTS (
                  SELECT 1 FROM dish_reviews dr WHERE dr.dish_id = d.id
                )
                """
            )

            print(f"\nTotal: +{total} reviews")
            if dry_run:
                raise _DryRunRollback("dry-run rollback")
        print("✓ done — recargá el mapa para ver Chef y Gem badges en CABA")
    except _DryRunRollback as e:
        print(f"(dry-run — sin cambios) {e}")
    finally:
        await conn.close()


class _DryRunRollback(Exception):
    pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()
