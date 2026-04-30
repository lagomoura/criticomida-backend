#!/usr/bin/env python3
"""Seed 5 critic users with reviews so the 'Siguiendo' feed has content.

Strategy
--------
- Crea 5 críticos legibles (Martín, Sofía, Tomás, Lucía, Diego) si no existen.
- Asigna 4 reviews por crítico sobre platos existentes (round-robin
  determinístico — cada re-run produce las mismas reviews).
- Uno de los 5 queda como "control sin reviews" para probar empty states
  (`diego@cc.test` por defecto).
- Variedad de pilares garantizada por crítico: al menos uno con
  `execution=3`, uno con `value_prop=3`, uno con `presentation=3` y uno sin
  pilares completos (texto legacy de la notificación).
- `admin@criticomida.com` (o el `--follower-email` que pases) sigue a los 5
  críticos para que su tab "Siguiendo" tenga material.
- Recalcula `dishes.computed_rating` y `dishes.review_count` al final.

Idempotente: re-ejecutarlo no duplica usuarios ni reviews.

Usage
-----
Inside the backend container (asyncpg ya está instalado):

    docker exec -e DATABASE_URL='postgresql+asyncpg://criticomida:criticomida_secret@db:5432/criticomida' \\
        backend-api-1 python scripts/seed_dev_critics.py [--dry-run] [--follower-email EMAIL]

Default password for all seeded critics: `critico123`.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

import asyncpg

# Pre-computed bcrypt hash for "critico123". Generated via:
#     from app.middleware.auth import hash_password
#     hash_password("critico123")
# Re-running bcrypt with random salt would produce a different but
# equally-valid hash; pinning one keeps the script stand-alone.
PASSWORD_HASH = "$2b$12$WYh6XOgDXazNTXTUNnWlh.ibWlPa6PPKPeJ18H.BBswnZAj2V41v6"

CRITICS: list[dict[str, str]] = [
    {
        "email": "martin@example.com",
        "display_name": "Martín Bouza",
        "handle": "martin_bouza",
        "bio": "Sigo el rastro de la milanesa perfecta.",
        "location": "Palermo",
    },
    {
        "email": "sofia@example.com",
        "display_name": "Sofía Castelli",
        "handle": "sofi_castelli",
        "bio": "Pasta hecha a mano > todo lo demás.",
        "location": "Villa Crespo",
    },
    {
        "email": "tomas@example.com",
        "display_name": "Tomás Ríos",
        "handle": "tomi_rios",
        "bio": "Asado, parrilla y discusiones de sobremesa.",
        "location": "San Telmo",
    },
    {
        "email": "lucia@example.com",
        "display_name": "Lucía Pérez",
        "handle": "luchi_perez",
        "bio": "Brunch, café de especialidad y postres.",
        "location": "Recoleta",
    },
    {
        "email": "diego@example.com",  # Control: no recibe reviews.
        "display_name": "Diego Vázquez",
        "handle": "diego_vz",
        "bio": "Recién llegado al mundo de las reseñas.",
        "location": "Caballito",
    },
]

KEEP_EMPTY_EMAIL = "diego@example.com"

NOTES_POOL: list[str] = [
    "Una de las mejores experiencias del mes. La cocina sabe lo que hace.",
    "Cumple, pero no me voló la cabeza. Volvería por la atmósfera.",
    "Sorpresa total: pedí esperando lo de siempre y me trajeron otra cosa.",
    "Justo lo que necesitaba un viernes después del laburo. Ambiente cálido.",
    "Precio justo para lo que recibís. La porción rinde para dos.",
    "Punto de cocción perfecto. Se nota la mano del chef detrás.",
    "La presentación no me convenció pero el sabor compensa.",
    "Probé con amigos y todos coincidimos: vale el viaje hasta acá.",
    "Para mí top 5 del barrio en su rubro. Sin dudas vuelvo.",
    "Esperaba más por el precio. Bien ejecutado pero no memorable.",
    "Si te gusta lo clásico, este lugar lo borda. Sin reinvenciones.",
    "Innovador en cada bocado. Los maridajes están pensados.",
]

# (presentation, value_prop, execution) — una plantilla por review por crítico.
PILLAR_PATTERNS: list[tuple[int | None, int | None, int | None]] = [
    (None, None, 3),   # Notif enriquecida → Ejecución 👨‍🍳
    (3, None, None),   # Notif enriquecida → Presentación 🌟
    (None, 3, None),   # Notif enriquecida → hallazgo 💎
    (2, 2, 2),         # Sin pilares en 3 → texto legacy
]


def normalize_dsn(url: str) -> str:
    """asyncpg no entiende `postgresql+asyncpg://`. Le sacamos el driver."""
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://") :]
    return url


async def run(dry_run: bool, follower_email: str) -> None:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        sys.exit("missing env var: DATABASE_URL")
    dsn = normalize_dsn(raw_url)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        async with conn.transaction():
            # 1) Verificar follower.
            follower_id = await conn.fetchval(
                "SELECT id FROM users WHERE email = $1", follower_email
            )
            if follower_id is None:
                sys.exit(f"follower email '{follower_email}' no existe en la DB.")

            # 2) Upsert críticos.
            critic_ids: dict[str, uuid.UUID] = {}
            for c in CRITICS:
                await conn.execute(
                    """
                    INSERT INTO users
                        (id, email, password_hash, display_name, handle,
                         bio, location, role, created_at, updated_at)
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7, 'critic', NOW(), NOW())
                    ON CONFLICT (email) DO NOTHING
                    """,
                    uuid.uuid4(),
                    c["email"],
                    PASSWORD_HASH,
                    c["display_name"],
                    c["handle"],
                    c["bio"],
                    c["location"],
                )
                cid = await conn.fetchval(
                    "SELECT id FROM users WHERE email = $1", c["email"]
                )
                critic_ids[c["email"]] = cid

            # 3) Dishes existentes.
            dish_rows = await conn.fetch(
                "SELECT id FROM dishes ORDER BY name, id"
            )
            dish_ids = [r["id"] for r in dish_rows]
            if len(dish_ids) < 16:
                sys.exit(
                    f"Necesito al menos 16 dishes y hay solo {len(dish_ids)}. "
                    "Sembrá restaurantes/platos primero."
                )

            # 4) Reviews por crítico (excepto el control).
            review_count = 0
            review_rating = {0: 4.5, 1: 4.0, 2: 4.5, 3: 3.5}
            for i, c in enumerate(CRITICS):
                if c["email"] == KEEP_EMPTY_EMAIL:
                    continue
                critic_id = critic_ids[c["email"]]
                offset = i * 4
                chosen = [dish_ids[(offset + j) % len(dish_ids)] for j in range(4)]
                for j, dish_id in enumerate(chosen):
                    presentation, value_prop, execution = PILLAR_PATTERNS[j]
                    note = NOTES_POOL[(i * 4 + j) % len(NOTES_POOL)]
                    rating = review_rating[j]
                    # Las primeras reviews del primer crítico son más viejas;
                    # las últimas del último crítico, más nuevas. Eso hace
                    # que sort=top reordene visiblemente respecto a recent.
                    minutes_back = (4 - i) * 4 * 30 - j * 30
                    inserted = await conn.fetchval(
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
                        uuid.uuid4(),
                        dish_id,
                        critic_id,
                        note,
                        rating,
                        presentation,
                        value_prop,
                        execution,
                        minutes_back,
                    )
                    if inserted is not None:
                        review_count += 1

            # 5) Follows: follower_email → cada crítico.
            follow_count = 0
            for c in CRITICS:
                critic_id = critic_ids[c["email"]]
                if critic_id == follower_id:
                    continue
                result = await conn.execute(
                    """
                    INSERT INTO follows (follower_id, following_id, created_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT DO NOTHING
                    """,
                    follower_id,
                    critic_id,
                )
                # asyncpg devuelve "INSERT 0 N" — N=1 si insertó algo nuevo.
                if result.endswith(" 1"):
                    follow_count += 1

            # 6) Recalcular agregados de dishes con reviews tocadas.
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

            print(
                f"Plan: 5 críticos ({len([c for c in CRITICS if c['email'] != KEEP_EMPTY_EMAIL])} con reviews, "
                f"1 control vacío: {KEEP_EMPTY_EMAIL})"
            )
            print(
                f"      +{review_count} reviews nuevas, "
                f"+{follow_count} follows nuevos desde {follower_email}"
            )

            if dry_run:
                # Rollback explícito sale del with-transaction al lanzar.
                raise _DryRunRollback("dry-run — rolling back")

        print("✓ done — login con cualquier crítico usando password 'critico123'")
    except _DryRunRollback as e:
        print(f"(dry-run — sin cambios) {e}")
    finally:
        await conn.close()


class _DryRunRollback(Exception):
    pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--follower-email",
        default="admin@criticomida.com",
        help="Email del usuario que sigue a los 5 críticos.",
    )
    args = ap.parse_args()
    asyncio.run(run(args.dry_run, args.follower_email))


if __name__ == "__main__":
    main()
