#!/usr/bin/env python3
"""Generar 1 imagen por review para los 4 críticos sembrados por
`seed_dev_critics.py`, usando fal.ai (flux-schnell) — mismo proveedor y
endpoint que `seed_review_images.py`.

Diferencia clave respecto al script genérico: el "estilo visual" se fija por
crítico (no es random por dish). Eso hace que cada crítico tenga una firma
fotográfica reconocible en el feed:

    - Martín Bouza   → phone_casual    (iPhone candid, vibrante)
    - Sofía Castelli → minimal_white   (editorial, cerámica blanca, limpio)
    - Tomás Ríos     → ambient_dim     (parrilla, bar, luz cálida moody)
    - Lucía Pérez    → outdoor_daylight (terraza, brunch, luz natural)

El "qué" (tipo de plato) lo dictan las DISH_HINTS — keywords reusadas tal cual
del script previo. Si no matchea ningún keyword, cae en un fallback genérico
con el nombre del plato.

Idempotente: si una review ya tiene al menos una imagen, se la salta.

Usage
-----
    docker exec -e DATABASE_URL='postgresql+asyncpg://criticomida:criticomida_secret@db:5432/criticomida' \\
        backend-api-1 python scripts/seed_critic_review_images.py [--dry-run]

`FAL_KEY` ya está en el ambiente del container (`backend/.env`).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import urllib.request
import uuid
from urllib.error import HTTPError, URLError

import asyncpg

FAL_ENDPOINT = "https://fal.run/fal-ai/flux/schnell"

# Estilos por crítico — handle (sin email para que sea fácil de leer aquí).
# Cada estilo es (template, image_size). El template usa {dish} y {hint}.
CRITIC_STYLES: dict[str, tuple[str, str]] = {
    "martin_bouza": (
        "iPhone snapshot of \"{dish}\". {hint}. Candid, slightly tilted angle, "
        "natural restaurant lighting, plate seen from a diner's perspective, "
        "shallow depth of field, vibrant colors, no text, no watermark.",
        "portrait_4_3",
    ),
    "sofi_castelli": (
        "Editorial food photo of \"{dish}\" on a clean white ceramic plate. "
        "{hint}. Minimal styling, soft diffuse light, neutral background, "
        "elegant plating, magazine quality, no text.",
        "square",
    ),
    "tomi_rios": (
        "Photo of \"{dish}\" served at a cozy parrilla restaurant. {hint}. "
        "Dim moody ambient lighting, dark wooden table, warm bulb glow, "
        "blurred amber bokeh background, side angle, atmospheric, no text.",
        "landscape_4_3",
    ),
    "luchi_perez": (
        "Photo of \"{dish}\" on a cafe table outdoors during brunch. {hint}. "
        "Bright midday sunlight, slight overexposure, fresh casual vibe, "
        "plates and napkins around, latte art nearby, no text.",
        "landscape_4_3",
    ),
}

# Keywords → hint. Idéntico al de `seed_review_images.py` para mantener
# consistencia de prompts con el resto del seed.
DISH_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("fugazzeta", "fugazza", "muzzarella", "muzza", "napolitana",
      "calabresa", "pizza", "gringa"),
     "Argentine-style pizza with thick crust, melted mozzarella, tomato sauce, "
     "served on a round metal tray"),
    (("milanesa", "mila"),
     "Argentine breaded veal cutlet (milanesa), golden crispy crust, served "
     "flat on a plate, sometimes with fries or salad"),
    (("empanada",),
     "Argentine empanada — half-moon savory pastry with golden baked crust"),
    (("asado", "parrilla", "tira", "vacio", "bife", "chorizo", "morcilla",
      "entraña"),
     "Argentine grilled meat from the parrilla, charred outside, juicy inside, "
     "served on a wooden board with chimichurri"),
    (("chicken", "pollo"),
     "Roasted or grilled chicken plate, golden crispy skin, served with sides"),
    (("ramen",),
     "Japanese ramen — bowl of noodles in rich broth, chashu pork slices, "
     "soft-boiled egg, scallions and nori"),
    (("burrito",),
     "Mexican burrito — large flour tortilla wrapped tightly around rice, "
     "beans, meat and salsa, cut in half"),
    (("taco",),
     "Mexican taco — soft corn tortilla folded around grilled meat, topped "
     "with onion, cilantro, lime wedge"),
    (("arepa",),
     "Venezuelan arepa — round corn cake split open and stuffed with cheese "
     "or meat, served warm"),
    (("brunch",),
     "Restaurant brunch plate — eggs, toast, fresh fruit, coffee on the side"),
    (("café turco", "turkish coffee"),
     "Turkish coffee in a small porcelain cup with a piece of lokum and "
     "decorative gold tray"),
    (("café", "cafe", "espresso", "capuccino", "cappuccino"),
     "Specialty coffee — espresso or cappuccino in a ceramic cup with latte "
     "art on a saucer"),
    (("ipa", "stout", "lager", "weizze", "weizen", "beer", "cerveza"),
     "Craft beer in a tall pint glass, golden or amber color, foamy head, "
     "condensation on the glass"),
    (("açai", "acai", "helado"),
     "Frozen dessert in a bowl with colorful fresh toppings, glossy"),
    (("kanafeh", "baklava", "malabi"),
     "Middle Eastern dessert, intricate plating, syrup glaze, decorative "
     "pistachios"),
    (("javali", "wild boar"),
     "Slow-cooked wild boar dish, dark rich sauce, side of polenta or potatoes"),
]


def normalize_dsn(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://") :]
    return url


def build_hint(dish_name: str) -> str:
    name_lower = dish_name.lower()
    for keys, hint in DISH_HINTS:
        if any(k in name_lower for k in keys):
            return hint
    # Fallback: el modelo improvisa con el nombre nomás.
    return f"plated dish, restaurant presentation"


def build_prompt(handle: str, dish_id: str, dish_name: str) -> tuple[str, str, int]:
    template, image_size = CRITIC_STYLES[handle]
    hint = build_hint(dish_name)
    prompt = template.format(dish=dish_name, hint=hint)
    seed = int(hashlib.sha1(dish_id.encode()).hexdigest()[:8], 16)
    return prompt, image_size, seed


def fal_generate(fal_key: str, prompt: str, image_size: str,
                 seed: int) -> str | None:
    body = json.dumps({
        "prompt": prompt,
        "image_size": image_size,
        "num_images": 1,
        "num_inference_steps": 4,
        "seed": seed,
    }).encode()
    req = urllib.request.Request(
        FAL_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Key {fal_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
    except HTTPError as e:
        print(f"  fal HTTP {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"  fal network error: {e}", file=sys.stderr)
        return None
    images = data.get("images") or []
    return images[0]["url"] if images else None


async def run(dry_run: bool) -> None:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        sys.exit("missing env var: DATABASE_URL")
    dsn = normalize_dsn(raw_url)

    fal_key = os.environ.get("FAL_KEY")
    if not fal_key and not dry_run:
        sys.exit("FAL_KEY not set in environment (check backend/.env).")

    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT u.handle, dr.id, d.id AS dish_id, d.name AS dish_name
            FROM dish_reviews dr
            JOIN users u ON u.id = dr.user_id
            JOIN dishes d ON d.id = dr.dish_id
            WHERE u.email LIKE 'martin@example.com'
               OR u.email LIKE 'sofia@example.com'
               OR u.email LIKE 'tomas@example.com'
               OR u.email LIKE 'lucia@example.com'
               OR u.email LIKE 'diego@example.com'
              AND NOT EXISTS (
                SELECT 1 FROM dish_review_images dri
                 WHERE dri.dish_review_id = dr.id
              )
            ORDER BY u.handle, dr.created_at
            """
        )
        if not rows:
            print("Nothing to do — todas las reviews ya tienen al menos una imagen.")
            return

        print(f"Plan: generar {len(rows)} imágenes (1 por review).")
        ok = fail = 0
        for i, r in enumerate(rows, 1):
            handle = r["handle"]
            review_id = r["id"]
            dish_id = str(r["dish_id"])
            dish_name = r["dish_name"]

            if handle not in CRITIC_STYLES:
                print(f"[{i}/{len(rows)}] skip {handle}: no style defined")
                continue

            prompt, image_size, seed = build_prompt(handle, dish_id, dish_name)
            style_label = handle.split("_")[0]
            print(f"[{i}/{len(rows)}] [{style_label}] {dish_name}")
            print(f"           prompt: {prompt[:120]}…")

            if dry_run:
                continue

            url = fal_generate(fal_key, prompt, image_size, seed)
            if not url:
                fail += 1
                continue

            await conn.execute(
                """
                INSERT INTO dish_review_images
                    (id, dish_review_id, url, alt_text, display_order, uploaded_at)
                VALUES ($1, $2, $3, $4, 0, NOW())
                """,
                uuid.uuid4(),
                review_id,
                url,
                dish_name[:300],
            )
            ok += 1
            time.sleep(0.4)  # cortesía con la API

        if dry_run:
            print("(dry-run — no DB writes, no fal calls)")
        else:
            print(f"\nDone. ok={ok} fail={fail}")
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()
