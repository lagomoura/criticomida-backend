#!/usr/bin/env python3
"""Seed comments, likes, and bookmarks across all reviews so the feed looks
populated for the demo. Idempotent: existing rows are preserved (likes /
bookmarks via ON CONFLICT, comments via per-review thresholds).

Distribution is intentionally uneven — a handful of "popular" reviews get
high engagement, most get a modest amount, a few stay quiet. This matches
the long-tail shape you'd see in a real feed.

Usage:
    DATABASE_URL='postgresql://…' \
        python scripts/seed_engagement.py [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import subprocess
import sys

# Spanish/Argentine flavored comments — purposely conversational, not all
# positive, mix of questions, reactions, follow-ups.
COMMENT_POOL: list[str] = [
    "¡Se ve increíble! Tengo que ir.",
    "Qué pinta, hace meses que quiero probar este lugar.",
    "Confirmo, una de las mejores de la zona 👌",
    "Yo fui el mes pasado, coincido 100%.",
    "¿Cuánto te salió aprox?",
    "¿Andan bien con celíacos?",
    "Lo mejor del barrio, sin dudas.",
    "No me convenció tanto la última vez, igual lo intento de nuevo.",
    "¿Aceptan reservas?",
    "Mejor relación precio-calidad de la zona.",
    "¿El postre lo probaron?",
    "Gracias por la reseña, me cerró ir.",
    "Mañana me acerco, no doy más.",
    "Qué hambre dió mirar esto 😂",
    "Yo lo pediría sin cebolla, pero igual tienta.",
    "Re recomendado!",
    "Justo buscaba algo así, gracias!",
    "¿Tienen opciones veggies?",
    "Buenísima reseña, muy detallada.",
    "Va directo a la lista de pendientes.",
    "Lo probé el sábado y te juro que es así.",
    "Estuve hace dos semanas, todo lo que decís es cierto.",
    "Re calidad/precio para una salida con amigos.",
    "¿Hicieron delivery alguna vez?",
    "Coincido pero la atención me dejó pensando.",
    "Pucha, esperaba más por el precio.",
    "El ambiente es lo mejor, además de la comida.",
    "El bondi de la zona es mortal, pero vale la pena.",
    "Para mí es top 3 de Buenos Aires en el rubro.",
    "¿Está abierto los domingos al mediodía?",
    "Me tinca, este finde caigo seguro.",
    "Linda foto, parece sacada de revista 📸",
    "El maridaje con la cerveza tira‑me data.",
    "Probaste el del menú ejecutivo? Es otro nivel.",
    "Cualquier crítica que hagas, banco — sos honesto siempre.",
    "Iba a ir el viernes, ahora seguro.",
    "Si pasás de nuevo avisá, vamos juntos.",
    "Hace cuánto que no voy 🥲, gracias por recordarme.",
    "Joya. Salí corriendo a sumar a la lista.",
]

# Helpers ---------------------------------------------------------------------


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"missing env var: {name}")
    return val


def psql(db_url: str, sql: str, *, tabular: bool = False) -> str:
    args = ["psql", db_url, "-v", "ON_ERROR_STOP=1", "-X", "-q"]
    if tabular:
        args += ["-A", "-t", "-F", "\t"]
    args += ["-c", sql]
    out = subprocess.run(args, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        sys.exit(f"psql failed:\nSQL: {sql[:200]}…\nSTDERR: {out.stderr}")
    return out.stdout


def sql_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# Distribution shape ----------------------------------------------------------
# For each review, deterministic from review_id so re-runs land identically.

def engagement_for(review_id: str) -> tuple[int, int, int]:
    """Return (n_likes, n_comments, n_bookmarks) for this review."""
    seed = int(hashlib.sha1(review_id.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    bucket = rng.random()
    if bucket < 0.10:        # ~10% popular
        return rng.randint(15, 28), rng.randint(3, 6), rng.randint(6, 12)
    if bucket < 0.40:        # ~30% medium
        return rng.randint(5, 14), rng.randint(1, 3), rng.randint(2, 5)
    if bucket < 0.85:        # ~45% low
        return rng.randint(1, 5), rng.randint(0, 2), rng.randint(0, 2)
    return 0, 0, 0           # ~15% silent


# Main ------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_url = env("DATABASE_URL")

    reviews_raw = psql(db_url, """
        SELECT dr.id::text, dr.user_id::text
        FROM dish_reviews dr
        ORDER BY dr.created_at
    """, tabular=True)
    reviews = [
        tuple(line.split("\t"))
        for line in reviews_raw.splitlines() if line.strip()
    ]

    users_raw = psql(db_url, "SELECT id::text FROM users",
                     tabular=True)
    user_ids = [u for u in users_raw.splitlines() if u.strip()]

    if not user_ids:
        sys.exit("no users in DB — seed users first")
    if not reviews:
        sys.exit("no reviews in DB")

    print(f"{len(reviews)} reviews, {len(user_ids)} users")

    plan_total = {"likes": 0, "comments": 0, "bookmarks": 0}
    operations: list[str] = []

    for review_id, author_id in reviews:
        n_likes, n_comments, n_bookmarks = engagement_for(review_id)
        plan_total["likes"] += n_likes
        plan_total["comments"] += n_comments
        plan_total["bookmarks"] += n_bookmarks

        seed = int(hashlib.sha1(review_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)

        # Engagement comes from anyone except the author.
        candidates = [u for u in user_ids if u != author_id]
        rng.shuffle(candidates)

        likers = candidates[:n_likes]
        bookmarkers = candidates[:n_bookmarks]  # may overlap with likers, fine
        commenters = candidates[:n_comments]    # one comment per user/review

        for uid in likers:
            operations.append(
                "INSERT INTO likes (user_id, review_id, created_at) VALUES "
                f"({sql_quote(uid)}, {sql_quote(review_id)}, NOW()) "
                "ON CONFLICT DO NOTHING;"
            )
        for uid in bookmarkers:
            operations.append(
                "INSERT INTO bookmarks (user_id, review_id, created_at) VALUES "
                f"({sql_quote(uid)}, {sql_quote(review_id)}, NOW()) "
                "ON CONFLICT DO NOTHING;"
            )
        for uid in commenters:
            body = rng.choice(COMMENT_POOL)
            operations.append(
                "INSERT INTO comments "
                "  (id, review_id, user_id, body, created_at, updated_at) "
                f"SELECT gen_random_uuid(), {sql_quote(review_id)}, "
                f"{sql_quote(uid)}, {sql_quote(body)}, NOW(), NOW() "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM comments c "
                f"   WHERE c.review_id = {sql_quote(review_id)} "
                f"     AND c.user_id = {sql_quote(uid)} "
                f"     AND c.body = {sql_quote(body)}"
                ");"
            )

    print(f"Plan: ~{plan_total['likes']} likes, "
          f"~{plan_total['comments']} comments, "
          f"~{plan_total['bookmarks']} bookmarks")

    if args.dry_run:
        print("(dry-run — no DB writes)")
        return

    # Run all operations in a single transaction.
    sql = "BEGIN;\n" + "\n".join(operations) + "\nCOMMIT;"
    psql(db_url, sql)
    print("✓ done")


if __name__ == "__main__":
    main()
