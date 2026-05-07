"""Generate cover images for category rows via fal.ai (flux/schnell) and save
the returned URL into `categories.image_url`.

Idempotent by default (`only_missing=True` skips rows that already have an
image). Pass `--all` to regenerate every category — useful if you change the
prompt or want fresh images.

Run from `backend/`:

    docker compose exec api python scripts/seed_category_images.py
    docker compose exec api python scripts/seed_category_images.py --all
    docker compose exec api python scripts/seed_category_images.py --slugs italiana,vietnamita

Requires `FAL_KEY` in env (already configured in dev/prod).

Why a script and not the migration: 52 fal.ai calls take ~2-3 min and consume
credit. We don't want that running on every `alembic upgrade head` (Railway
would re-run the migration with `ON CONFLICT DO NOTHING` but the API would
also try to regenerate images on a no-op upgrade). One-shot manual call.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import async_session  # noqa: E402
from app.models.category import Category  # noqa: E402

FAL_URL = "https://fal.run/fal-ai/flux/schnell"
PROMPT_TPL = (
    "Food category cover photo for {name} cuisine, top-down view, vibrant "
    "colors, professional food photography, clean background"
)


async def _generate_one(http: httpx.AsyncClient, name: str, fal_key: str) -> str | None:
    try:
        resp = await http.post(
            FAL_URL,
            headers={
                "Authorization": f"Key {fal_key}",
                "Content-Type": "application/json",
            },
            json={
                "prompt": PROMPT_TPL.format(name=name),
                "image_size": "square",
                "num_images": 1,
                "num_inference_steps": 4,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("images", [{}])[0].get("url")
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        print(f"  ✗ {name!r}: {exc}", file=sys.stderr)
        return None


async def run(*, only_missing: bool, slugs_filter: set[str] | None) -> None:
    fal_key = os.environ.get("FAL_KEY")
    if not fal_key:
        print("ERROR: FAL_KEY no está seteada en el entorno.", file=sys.stderr)
        sys.exit(1)

    async with async_session() as db, httpx.AsyncClient() as http:
        rows = (
            await db.execute(
                select(Category).order_by(Category.display_order, Category.name)
            )
        ).scalars().all()

        if slugs_filter:
            rows = [c for c in rows if c.slug in slugs_filter]

        targets = [c for c in rows if not (only_missing and c.image_url)]
        print(
            f"{len(targets)} categorías a procesar "
            f"({'solo faltantes' if only_missing else 'todas'})\n"
        )

        ok = fail = 0
        for cat in targets:
            url = await _generate_one(http, cat.name, fal_key)
            if url:
                cat.image_url = url
                await db.commit()
                ok += 1
                print(f"  ✓ {cat.slug:20s} → {url[:80]}…")
            else:
                fail += 1

        print(f"\nDone. {ok} OK, {fail} falló.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate fal.ai cover images for categories."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Regenerar también las que ya tienen imagen.",
    )
    parser.add_argument(
        "--slugs",
        type=str,
        default=None,
        help="Lista coma-separada de slugs a procesar (default: todos).",
    )
    args = parser.parse_args()

    slugs_filter: set[str] | None = None
    if args.slugs:
        slugs_filter = {s.strip() for s in args.slugs.split(",") if s.strip()}

    asyncio.run(
        run(only_missing=not args.all, slugs_filter=slugs_filter)
    )


if __name__ == "__main__":
    main()
