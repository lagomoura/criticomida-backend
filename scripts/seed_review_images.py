#!/usr/bin/env python3
"""Populate dish covers and review images, preferring real Google Places photos
when the restaurant has them, falling back to fal.ai (flux-schnell) only for
dishes whose restaurant lacks `google_photos`.

Modes:
    --reset       Clear existing covers + dish_review_images before running
                  (useful when a previous run produced bad AI images).
    --only-ai     Skip Google photos, regenerate everything via fal.ai.
    --dry-run     Print what would happen without touching DB or fal.

Usage:
    FAL_KEY=... DATABASE_URL='postgresql://…' \
        python scripts/seed_review_images.py [--reset]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from urllib.error import HTTPError, URLError

FAL_ENDPOINT = "https://fal.run/fal-ai/flux/schnell"

# Lightweight keyword hints — when a dish name matches a key, we inject the
# value as a strong style anchor in the fal prompt. Gives the model a much
# better chance of producing the right kind of food than relying on the
# restaurant's category alone (which can be vague like "international").
DISH_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("fugazzeta", "fugazza", "muzzarella", "muzza", "napolitana",
      "calabresa", "pizza"),
     "Argentine-style pizza with thick crust, melted mozzarella, tomato sauce, "
     "served on a round metal tray"),
    (("milanesa", "mila"),
     "Argentine breaded veal cutlet (milanesa), golden crispy crust, served "
     "flat on a plate, sometimes with fries or salad"),
    (("empanada",),
     "Argentine empanada — half-moon savory pastry with golden baked crust, "
     "stuffed and crimped on the edge"),
    (("asado", "parrilla", "tira", "vacio", "bife", "chorizo", "morcilla",
      "entraña"),
     "Argentine grilled meat from the parrilla, charred outside, juicy inside, "
     "served on a wooden board with chimichurri"),
    (("ramen",),
     "Japanese ramen — bowl of noodles in rich broth, chashu pork slices, "
     "soft-boiled egg, scallions and nori"),
    (("sushi", "nigiri", "maki", "sashimi"),
     "Japanese sushi — fresh raw fish over rice, neat plating on a slate or "
     "wooden board"),
    (("taco",),
     "Mexican taco — soft corn tortilla folded around grilled meat, topped "
     "with onion, cilantro, lime wedge"),
    (("burrito",),
     "Mexican burrito — large flour tortilla wrapped tightly around rice, "
     "beans, meat and salsa, cut in half"),
    (("quesadilla",),
     "Mexican quesadilla — folded tortilla filled with melted cheese, "
     "golden grilled exterior"),
    (("kebab", "shawarma"),
     "Middle Eastern kebab — grilled meat on flatbread or pita with vegetables "
     "and tahini sauce"),
    (("tabule", "tabbouleh"),
     "Levantine tabbouleh — fresh parsley, tomato, bulgur, mint, lemon"),
    (("hummus",),
     "Middle Eastern hummus — creamy chickpea dip drizzled with olive oil, "
     "topped with paprika and parsley, served with pita"),
    (("malabi", "kanafeh", "baklava"),
     "Middle Eastern dessert, intricate plating, syrup glaze, decorative "
     "pistachios"),
    (("brunch",),
     "Restaurant brunch plate — eggs, toast, fresh fruit, coffee on the side, "
     "natural daylight on a wooden table"),
    (("cerveza", "ipa", "stout", "lager", "weizze", "weizen", "beer"),
     "Craft beer in a tall pint glass, golden or amber color, foamy head, "
     "condensation on the glass, pub setting"),
    (("helado", "açai", "acai"),
     "Frozen dessert in a bowl or cup, colorful fresh toppings, glossy"),
    (("café", "cafe", "espresso", "capuccino", "cappuccino"),
     "Specialty coffee — espresso or cappuccino in a ceramic cup with latte "
     "art on a saucer"),
    (("ensalada", "salad"),
     "Fresh salad bowl, mixed greens, vibrant vegetables, drizzled dressing"),
    (("hamburguesa", "burger"),
     "Gourmet burger — toasted bun, melted cheese, lettuce, tomato, juicy "
     "patty, served with fries on the side"),
    (("pasta", "spaghetti", "fettuccine", "ravioli", "ñoqui", "ñoquis",
      "gnocchi"),
     "Italian-style pasta dish, twirled noodles, sauce coating, parmesan, "
     "fresh basil"),
    (("poke",),
     "Hawaiian poke bowl — cubed raw fish over rice with avocado, edamame, "
     "seaweed and sesame seeds"),
]

# Generic category-based fallback when no dish-name keyword matches.
CATEGORY_HINTS: dict[str, str] = {
    "Pizzas": "Argentine-style pizza on a round metal tray",
    "Pizzería": "Argentine-style pizza on a round metal tray",
    "Mexicana": "Mexican food, vibrant colors, salsas, tortilla on the side",
    "Japonesa": "Japanese cuisine, neat presentation, chopsticks beside",
    "Árabe": "Middle Eastern cuisine, warm spices, herbs, pita bread",
    "Heladería": "Frozen dessert in a bowl, colorful toppings",
    "Brunchs": "Brunch plate with eggs, toast, fruit, coffee",
    "Desayunos": "Breakfast plate, eggs, toast, coffee",
    "Burguers": "Gourmet burger with fries on the side",
    "Coreana": "Korean banchan and grilled meat presentation",
    "Israelí": "Israeli cuisine — hummus, pita, fresh herbs",
    "Tailandesa": "Thai food, fresh herbs, lime, chili",
    "Brasileña": "Brazilian dish, tropical colors, rice and beans aesthetic",
    "Peruana": "Peruvian dish — ceviche or causa style, fresh seafood, lime",
    "China": "Chinese cuisine, wok-tossed, glossy sauce, white rice on the side",
    "Dulces": "Bakery sweet — pastry or cake, glossy glaze, decorative plating",
    "Parrillas": "Argentine grilled meat board with chimichurri",
}

# Each style is a (label, template, image_size) tuple. Pick one
# deterministically from the dish_id so a re-run gives the same image and the
# feed keeps its visual variety. Templates are written so the dish hint slots
# in naturally — they avoid the same "professional food photography, top-down,
# wooden table" formula.
STYLE_TEMPLATES: list[tuple[str, str, str]] = [
    (
        "phone_casual",
        "iPhone snapshot of \"{dish}\". {hint}. Candid, slightly tilted angle, "
        "natural restaurant lighting, plate seen from a diner's perspective, "
        "shallow depth of field, vibrant colors, no text, no watermark.",
        "portrait_4_3",
    ),
    (
        "pro_overhead",
        "Professional overhead food photography of \"{dish}\". {hint}. "
        "Top-down flat lay on a wooden table, soft daylight, vibrant colors, "
        "magazine quality, hyper-detailed, no text.",
        "square",
    ),
    (
        "instagram_warm",
        "Instagram-style photo of \"{dish}\". {hint}. Hand of a friend holding "
        "the plate just above the table, warm Lightroom filter, soft golden "
        "hour glow, slight film grain, casual aesthetic, no text.",
        "portrait_4_3",
    ),
    (
        "ambient_dim",
        "Photo of \"{dish}\" served at a cozy restaurant. {hint}. Dim moody "
        "ambient lighting, dark wooden table, candle nearby, blurred warm "
        "bokeh background, side angle, atmospheric, no text.",
        "landscape_4_3",
    ),
    (
        "closeup_macro",
        "Macro close-up of \"{dish}\". {hint}. Shallow depth of field, "
        "extreme detail of the texture, ingredients visible, side angle, soft "
        "natural light, no text.",
        "landscape_4_3",
    ),
    (
        "outdoor_daylight",
        "Photo of \"{dish}\" on a cafe table outdoors. {hint}. Bright midday "
        "sunlight, slight overexposure, casual everyday vibe, plates and "
        "napkins around, no text.",
        "landscape_4_3",
    ),
    (
        "midmeal_messy",
        "Mid-meal photo of \"{dish}\" on a busy table. {hint}. Cutlery, "
        "drinks, crumpled napkin and breadcrumbs around the plate, natural "
        "indoor light, candid moment, no text.",
        "landscape_4_3",
    ),
    (
        "lowangle_neon",
        "Low-angle smartphone photo of \"{dish}\". {hint}. Restaurant interior "
        "with subtle neon or warm bulb lighting in the background, slight "
        "motion blur, vibrant night-out feel, no text.",
        "portrait_4_3",
    ),
    (
        "minimal_white",
        "Editorial food photo of \"{dish}\" on a clean white ceramic plate. "
        "{hint}. Minimal styling, soft diffuse light, neutral background, "
        "elegant plating, no text.",
        "square",
    ),
]


def pick_style(dish_id: str) -> tuple[str, str, str]:
    h = int(hashlib.sha1(dish_id.encode()).hexdigest(), 16)
    return STYLE_TEMPLATES[h % len(STYLE_TEMPLATES)]


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
        sys.exit(f"psql failed:\nSQL: {sql}\nSTDERR: {out.stderr}")
    return out.stdout


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


def sql_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_prompt(dish_id: str, dish: str,
                 category: str | None) -> tuple[str, str, str, int]:
    """Return (style_label, prompt, image_size, seed)."""
    name_lower = dish.lower()
    hint = None
    for keys, h in DISH_HINTS:
        if any(k in name_lower for k in keys):
            hint = h
            break
    if hint is None and category:
        hint = CATEGORY_HINTS.get(category)
    if hint is None:
        hint = f"Plated dish from {category or 'a restaurant'} cuisine"

    label, template, image_size = pick_style(dish_id)
    prompt = template.format(dish=dish, hint=hint)
    seed = int(hashlib.sha1(dish_id.encode()).hexdigest()[:8], 16)
    return label, prompt, image_size, seed


def reset(db_url: str) -> None:
    print("Resetting existing covers and review images…")
    psql(db_url, """
        BEGIN;
        DELETE FROM dish_review_images;
        UPDATE dishes SET cover_image_url = NULL;
        COMMIT;
    """)


def fetch_dishes(db_url: str) -> list[tuple[str, str, str, str | None, str | None]]:
    """Return rows of (dish_id, dish_name, restaurant_name, category, google_photo_url)."""
    rows = psql(db_url, """
        SELECT
            d.id::text,
            d.name,
            r.name AS restaurant,
            COALESCE(c.name, ''),
            COALESCE(r.google_photos->0->>'url', '')
        FROM dishes d
        JOIN restaurants r ON r.id = d.restaurant_id
        LEFT JOIN categories c ON c.id = r.category_id
        WHERE d.cover_image_url IS NULL
        ORDER BY r.id, d.created_at
    """, tabular=True)
    parsed: list[tuple[str, str, str, str | None, str | None]] = []
    for line in rows.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 5:
            print(f"WARN unexpected row: {line!r}", file=sys.stderr)
            continue
        dish_id, name, rest, cat, gphoto = parts
        parsed.append((dish_id, name, rest, cat or None, gphoto or None))
    return parsed


def attach(db_url: str, dish_id: str, dish_name: str, url: str) -> None:
    sql = f"""
    BEGIN;
    UPDATE dishes
       SET cover_image_url = {sql_quote(url)}
     WHERE id = {sql_quote(dish_id)};
    INSERT INTO dish_review_images
          (id, dish_review_id, url, alt_text, display_order, uploaded_at)
    SELECT gen_random_uuid(), dr.id, {sql_quote(url)},
           {sql_quote(dish_name)}, 0, NOW()
      FROM dish_reviews dr
     WHERE dr.dish_id = {sql_quote(dish_id)}
       AND NOT EXISTS (
         SELECT 1 FROM dish_review_images dri
          WHERE dri.dish_review_id = dr.id
       );
    COMMIT;
    """
    psql(db_url, sql)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true",
                    help="clear existing covers + review images before running")
    ap.add_argument("--only-ai", action="store_true",
                    help="skip google_photos, use fal.ai for everything")
    ap.add_argument("--dry-run", action="store_true",
                    help="print plan without writing")
    args = ap.parse_args()

    db_url = env("DATABASE_URL")
    fal_key = os.environ.get("FAL_KEY")  # only required if any dish needs AI

    if args.reset and not args.dry_run:
        reset(db_url)

    dishes = fetch_dishes(db_url)
    if not dishes:
        print("Nothing to do — every dish already has a cover_image_url.")
        return

    n_google = sum(1 for d in dishes if d[4] and not args.only_ai)
    n_ai = len(dishes) - n_google
    print(f"Plan: {len(dishes)} dishes total — {n_google} via Google Places, "
          f"{n_ai} via fal.ai")
    if args.dry_run:
        for dish_id, name, rest, cat, gphoto in dishes[:10]:
            if gphoto and not args.only_ai:
                print(f"  [google] {name} @ {rest} ({cat or '—'})")
            else:
                label, prompt, _size, _seed = build_prompt(dish_id, name, cat)
                print(f"  [fal/{label}] {name} @ {rest} ({cat or '—'})")
                print(f"           → {prompt[:140]}")
        if len(dishes) > 10:
            print(f"  …and {len(dishes) - 10} more")
        return

    ok_g = ok_f = fail = 0
    for i, (dish_id, name, rest, cat, gphoto) in enumerate(dishes, 1):
        if gphoto and not args.only_ai:
            print(f"[{i}/{len(dishes)}] [google] {name} @ {rest}")
            attach(db_url, dish_id, name, gphoto)
            ok_g += 1
            continue

        if not fal_key:
            sys.exit("FAL_KEY is required for AI fallback but is not set")
        label, prompt, image_size, seed = build_prompt(dish_id, name, cat)
        print(f"[{i}/{len(dishes)}] [fal/{label}] {name} @ {rest} "
              f"({cat or '—'})")
        print(f"           prompt: {prompt[:140]}")
        url = fal_generate(fal_key, prompt, image_size, seed)
        if not url:
            fail += 1
            continue
        attach(db_url, dish_id, name, url)
        ok_f += 1
        time.sleep(0.4)

    print(f"\nDone. google={ok_g} fal={ok_f} fail={fail}")


if __name__ == "__main__":
    main()
