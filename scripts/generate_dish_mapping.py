"""
Generate dish_mapping.yaml from Google Maps Takeout "Pratos" data.

Cross-references dish dates with restaurant review dates to auto-suggest
the most likely restaurant for each dish. Run from backend/:

    python scripts/generate_dish_mapping.py \\
        --db-url "postgresql+asyncpg://user:pass@localhost:5433/criticomida"

Then edit the generated dish_mapping.yaml and run import_dishes.py.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.restaurant import Restaurant

PRATOS_PATH = Path(
    "../google_maps_data/Takeout/Maps/"
    "Pratos, produtos e atividades adicionados/"
    "Pratos, produtos e atividades adicionados.json"
)
COMENTARIOS_PATH = Path(
    "../google_maps_data/Takeout/Maps (Seus lugares)/Comentários.json"
)
OUTPUT_PATH = Path("../google_maps_data/dish_mapping.yaml")

# ── Manual overrides ────────────────────────────────────────────────────────
# When auto-matching by date is ambiguous, these name-based hints take over.
# Key: substring of dish text (lowercase). Value: substring of restaurant name.
DISH_NAME_HINTS: dict[str, str] = {
    "ramen de cordero":   "juajua ramen",
    "ramen con cerdo":    "juajua ramen",
    "salmón pertutti":    "pertutti",
    "kebab turco":        "eretz",
    "tabule":             "eretz",
    "kanafeh":            "eretz",
    "malabi":             "eretz",
    "café turco":         "eretz",
    "menu ejecutivo":     "eretz",
    "totopos":            "che taco",
    "salsa verde":        "che taco",
    "tacos al pastor":    "che taco",
    "tacos de carne":     "che taco",
    "burritos":           "che taco",
    "gringa":             "che taco",
    "krakow sausage":     "dundalk",
    "ipa beer":           "cervecería untertürkheim",
    "weizze beer":        "cervecería untertürkheim",
    "ipa":                "glück",
    "javali":             "pertutti",
    "picada de mar":      "sacoa",
    "cerveza":            "bélgica bar",
    "brunch":             "1870 beer",
    "açai":               "hana poke",
    "chicken":            "mocozi",
}


def load_dishes(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["contributions"]


def load_restaurant_dates(path: Path) -> dict[str, list[str]]:
    """Returns {date_str: [restaurant_name, ...]} for all rated reviews."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    result: dict[str, list[str]] = {}
    for feat in data["features"]:
        props = feat.get("properties", {})
        if "location" not in props:
            continue
        if props.get("five_star_rating_published", 0) == 0:
            continue
        date = props["date"][:10]
        name = props["location"]["name"]
        result.setdefault(date, []).append(name)
    return result


def find_best_restaurant(
    dish_text: str,
    dish_date: str,
    restaurant_dates: dict[str, list[str]],
    db_restaurants: list[tuple[str, str]],  # [(name, slug)]
) -> tuple[str, str]:
    """
    Returns (restaurant_name, confidence) where confidence is:
      'high'   - name hint matched
      'date'   - reviewed same day or ±1 day (single candidate)
      'date?'  - reviewed same day but multiple candidates
      '???'    - no match found
    """
    lower = dish_text.lower()

    # 1. Name-based hint (highest priority)
    for hint_key, hint_val in DISH_NAME_HINTS.items():
        if hint_key in lower:
            for name, slug in db_restaurants:
                if hint_val.lower() in name.lower():
                    return name, "high"
            # hint matched but restaurant not in DB
            return f"[not in DB: {hint_val}]", "high-missing"

    # 2. Same-day match
    candidates = restaurant_dates.get(dish_date, [])

    # Also check ±1 day
    dt = datetime.fromisoformat(dish_date)
    for delta in (-1, 1):
        nearby = (dt + timedelta(days=delta)).strftime("%Y-%m-%d")
        candidates += [f"{n} (±1d)" for n in restaurant_dates.get(nearby, [])]

    # Filter to only those actually in our DB
    db_names = {name.lower() for name, _ in db_restaurants}
    matched = [c for c in candidates if c.rstrip(" (±1d)").lower() in db_names or
               any(c.rstrip(" (±1d)").lower() in name.lower() for name in db_names)]

    if len(matched) == 1:
        clean = matched[0].replace(" (±1d)", "")
        return clean, "date"
    elif len(matched) > 1:
        return matched[0].replace(" (±1d)", ""), f"date? ({', '.join(c.replace(' (±1d)','') for c in matched[:3])})"

    return "???", "???"


def format_yaml_entry(
    idx: int,
    dish: dict,
    restaurant_name: str,
    confidence: str,
) -> str:
    date = dish["created"][:10]
    text = dish["text"].replace('"', '\\"')
    photo = dish["photo_url"]

    conf_comment = {
        "high": "# ✓ matched by dish name",
        "date": "# ✓ matched by review date",
        "???": "# ✗ unknown — please fill in",
    }.get(confidence, f"# ~ {confidence}")

    lines = [
        f"- id: {idx}",
        f'  dish: "{text}"',
        f"  date: {date}",
        f'  restaurant: "{restaurant_name}"  {conf_comment}',
        f'  note: ""          # optional: your notes about this dish',
        f"  rating: null      # optional: 1-5",
        f'  photo_url: "{photo}"',
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=None)
    args = parser.parse_args()

    db_url = args.db_url or settings.DATABASE_URL
    engine = create_async_engine(db_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        result = await session.execute(select(Restaurant.name, Restaurant.slug))
        db_restaurants = list(result.all())

    await engine.dispose()

    dishes = load_dishes(PRATOS_PATH)
    restaurant_dates = load_restaurant_dates(COMENTARIOS_PATH)

    lines = [
        "# dish_mapping.yaml — generated by generate_dish_mapping.py",
        "# Review each entry:",
        '#   - Confirm or fix the "restaurant" field (use the exact name from your DB)',
        '#   - Add a "note" if you remember something about the dish',
        '#   - Add a "rating" (1-5) if you want to create a DishReview',
        '#   - Set restaurant to "SKIP" to ignore this dish',
        "",
        "dishes:",
        "",
    ]

    unknown_count = 0
    for i, dish in enumerate(dishes, 1):
        date = dish["created"][:10]
        restaurant_name, confidence = find_best_restaurant(
            dish["text"], date, restaurant_dates, db_restaurants
        )
        if confidence == "???":
            unknown_count += 1
        lines.append(format_yaml_entry(i, dish, restaurant_name, confidence))

    content = "\n".join(lines)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8")

    print(f"Generated: {OUTPUT_PATH}")
    print(f"  Total dishes : {len(dishes)}")
    print(f"  Auto-matched : {len(dishes) - unknown_count}")
    print(f"  Need manual  : {unknown_count}  ← these have '???'")
    print(f"\nEdit {OUTPUT_PATH} then run:")
    print("  python scripts/import_dishes.py --db-url '...' --commit")


if __name__ == "__main__":
    asyncio.run(main())
