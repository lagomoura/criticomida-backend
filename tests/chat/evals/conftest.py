"""Pytest fixtures for chat eval suite.

The eval suite exercises the chat agents end-to-end against the real
Anthropic API and a controlled DB fixture. Each eval session creates a
small, predictable restaurant + reviews payload, runs the dataset against
it, and cleans up at session teardown.

The DB pattern mirrors ``tests/integration/conftest.py``: pytest data
gets a known prefix (``pytest_chat_eval_``) so the autouse cleanup can
delete it without touching seeded prod data.

The eval suite is **opt-in** because it costs real API tokens. Set
``RUN_CHAT_EVALS=1`` to enable, otherwise the tests skip with a clear
message.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select, text

from app.database import async_session, engine
from app.models.category import Category
from app.models.chat import (
    PriceBand,
    TastePillar,
    UserTasteProfile,
)
from app.models.dish import Dish, DishReview, PriceTier, SentimentLabel
from app.models.owner_content import DishReviewOwnerResponse
from app.models.restaurant import Restaurant
from app.models.user import User
from tests.chat.evals.runner import EvalCase


CHAT_EVAL_PLACE_ID = "pytest_chat_eval_main"
CHAT_EVAL_USER_PREFIX = "pytest_chat_eval_"
# Sommelier fixture spans 3 restaurants in 3 neighborhoods so the cleanup
# uses a LIKE pattern instead of an exact match on google_place_id.
CHAT_EVAL_PLACE_PREFIX = "pytest_chat_eval_"
CHAT_EVAL_SOMMELIER_PLACE_PREFIX = "pytest_chat_eval_sommelier_"


# ──────────────────────────────────────────────────────────────────────────
#   Gate
# ──────────────────────────────────────────────────────────────────────────


def _evals_enabled() -> bool:
    return os.environ.get("RUN_CHAT_EVALS") == "1"


def _api_key() -> str | None:
    return os.environ.get("CHAT_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")


@pytest.fixture(scope="session", autouse=True)
def _gate_chat_evals():
    if not _evals_enabled():
        pytest.skip(
            "Chat eval suite disabled. Set RUN_CHAT_EVALS=1 (and CHAT_API_KEY) to run."
        )
    if not _api_key():
        pytest.skip(
            "CHAT_API_KEY / ANTHROPIC_API_KEY not set; chat evals need a real model."
        )
    yield


# ──────────────────────────────────────────────────────────────────────────
#   DB fixture: restaurant + dishes + reviews
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class EvalFixtureScope:
    """Identifiers a case can use to build assertions if needed."""

    restaurant_id: str
    owner_user_id: str
    dish_ids: dict[str, str]  # dish name → uuid string
    review_ids_pending: list[str]
    review_ids_responded: list[str]


@pytest.fixture(scope="session")
async def chat_eval_scope() -> EvalFixtureScope:
    """Build a session-scoped fixture restaurant the eval suite reuses.

    The fixture is committed to the dev DB; the cleanup fixture below
    drops it after the session. We commit (rather than rolling back) so
    the agent loop's own sessions can read the data — the loop opens a
    fresh ``AsyncSession`` per request.
    """
    async with async_session() as session:
        owner = User(
            id=uuid.uuid4(),
            email=f"{CHAT_EVAL_USER_PREFIX}owner@test.com",
            password_hash="x" * 60,
            display_name="Eval Owner",
        )
        session.add(owner)

        restaurant = Restaurant(
            id=uuid.uuid4(),
            slug=f"pytest-chat-eval-{uuid.uuid4().hex[:6]}",
            name="Eval Restaurant",
            location_name="Av. Test 123, Buenos Aires",
            city="Buenos Aires",
            google_place_id=CHAT_EVAL_PLACE_ID,
            latitude=Decimal("-34.6"),
            longitude=Decimal("-58.4"),
            created_by=owner.id,
        )
        session.add(restaurant)
        await session.flush()  # need ids for FK below

        # ``name_normalized`` is a GENERATED ALWAYS column — Postgres
        # fills it from ``name``. We must not pass a value for it.
        dish_names = [
            "Hamburguesa Clásica",
            "Tacos al Pastor",
            "Risotto de Hongos",
            "Tiramisú",
        ]
        dish_ids: dict[str, str] = {}
        dish_objs: dict[str, Dish] = {}
        for name in dish_names:
            dish = Dish(
                id=uuid.uuid4(),
                restaurant_id=restaurant.id,
                name=name,
                computed_rating=Decimal("0"),
                review_count=0,
                created_by=owner.id,
            )
            session.add(dish)
            dish_ids[name] = str(dish.id)
            dish_objs[name] = dish

        await session.flush()

        # 12 reviews across the 4 dishes, mixing pending vs responded,
        # sentiments, and dates spread across April–May 2026.
        # Format: (dish_name, days_ago_anchor, rating, sentiment, sscore, note, has_response)
        anchor = date(2026, 5, 4)
        review_specs: list[tuple[str, int, float, SentimentLabel, float, str, bool]] = [
            # ── Pending (no owner response) ─────────────────────────────────
            ("Hamburguesa Clásica", 1, 5.0, SentimentLabel.positive, 0.92,
             "Excelente, mejor que nunca. La carne en su punto.", False),
            ("Tacos al Pastor", 1, 5.0, SentimentLabel.positive, 0.85,
             "Test", False),
            ("Risotto de Hongos", 1, 4.0, SentimentLabel.neutral, 0.10,
             "Cumple. Nada del otro mundo.", False),
            ("Tiramisú", 1, 2.0, SentimentLabel.negative, -0.78,
             "Muy seco, no me gustó. La crema sabía a nada.", False),
            ("Hamburguesa Clásica", 5, 4.0, SentimentLabel.positive, 0.65,
             "Buena, vuelvo. El pan es lo mejor.", False),
            ("Tacos al Pastor", 5, 3.5, SentimentLabel.neutral, 0.05,
             "Innovador en cada bocado. Los maridajes están pensados.", False),
            ("Risotto de Hongos", 8, 1.0, SentimentLabel.negative, -0.95,
             "Insípido y frío. No vuelvo.", False),
            ("Tiramisú", 8, 5.0, SentimentLabel.positive, 0.88,
             "El mejor tiramisú de la zona. Postre redondo.", False),
            # ── Responded (owner already replied) ──────────────────────────
            ("Hamburguesa Clásica", 18, 3.0, SentimentLabel.neutral, -0.05,
             "Está bien, sin más. La porción de papas chica.", True),
            ("Tacos al Pastor", 22, 4.0, SentimentLabel.positive, 0.55,
             "Sabroso pero salado. Bajen un toque la sal.", True),
            ("Risotto de Hongos", 25, 4.0, SentimentLabel.positive, 0.62,
             "Cremoso y gustoso. Buena relación calidad-precio.", True),
            ("Tiramisú", 30, 3.0, SentimentLabel.neutral, 0.0,
             "Ok pero no destaca. Esperaba más por el precio.", True),
        ]

        review_ids_pending: list[str] = []
        review_ids_responded: list[str] = []

        for idx, (
            dish_name,
            days_back,
            rating,
            sentiment,
            sscore,
            note,
            has_response,
        ) in enumerate(review_specs):
            reviewer = User(
                id=uuid.uuid4(),
                email=f"{CHAT_EVAL_USER_PREFIX}reviewer{idx}@test.com",
                password_hash="x" * 60,
                display_name=f"Eval Reviewer {idx}",
            )
            session.add(reviewer)

            review_date = date.fromordinal(anchor.toordinal() - days_back)
            review = DishReview(
                id=uuid.uuid4(),
                dish_id=dish_objs[dish_name].id,
                user_id=reviewer.id,
                date_tasted=review_date,
                note=note,
                rating=Decimal(str(rating)),
                sentiment_label=sentiment,
                sentiment_score=Decimal(str(sscore)),
                sentiment_analyzed_at=datetime.now(timezone.utc),
                created_at=datetime(
                    review_date.year,
                    review_date.month,
                    review_date.day,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            )
            session.add(review)
            await session.flush()

            if has_response:
                response = DishReviewOwnerResponse(
                    review_id=review.id,
                    owner_user_id=owner.id,
                    body="Gracias por tu feedback, lo tomamos en cuenta.",
                )
                session.add(response)
                review_ids_responded.append(str(review.id))
            else:
                review_ids_pending.append(str(review.id))

        await session.commit()

        return EvalFixtureScope(
            restaurant_id=str(restaurant.id),
            owner_user_id=str(owner.id),
            dish_ids=dish_ids,
            review_ids_pending=review_ids_pending,
            review_ids_responded=review_ids_responded,
        )


@pytest.fixture(scope="session", autouse=True)
async def _cleanup_chat_eval_data(_gate_chat_evals):
    """Drop pytest_chat_eval_* records once the session ends.

    Order: profile-shaped tables first, then restaurants (cascades
    dishes → reviews → owner_responses), then users (cascades anything
    else FK-bound to them). Both Business and Sommelier scopes share
    the ``CHAT_EVAL_PLACE_PREFIX`` prefix on ``google_place_id`` so the
    LIKE pattern catches them in one pass.
    """
    yield
    if not _evals_enabled():
        return
    async with engine.begin() as conn:
        # owner_chat_preferences cascades from users/restaurants; we
        # delete it explicitly anyway in case the cascade order leaves
        # orphans during a partial failure.
        await conn.execute(
            text(
                "DELETE FROM owner_chat_preferences "
                "WHERE user_id IN (SELECT id FROM users WHERE email LIKE :prefix) "
                "OR restaurant_id IN (SELECT id FROM restaurants WHERE google_place_id LIKE :pid_pattern)"
            ),
            {
                "pid_pattern": f"{CHAT_EVAL_PLACE_PREFIX}%",
                "prefix": f"{CHAT_EVAL_USER_PREFIX}%@test.com",
            },
        )
        await conn.execute(
            text(
                "DELETE FROM user_taste_profiles "
                "WHERE user_id IN (SELECT id FROM users WHERE email LIKE :prefix)"
            ),
            {"prefix": f"{CHAT_EVAL_USER_PREFIX}%@test.com"},
        )
        await conn.execute(
            text(
                "DELETE FROM restaurants WHERE google_place_id LIKE :pid_pattern"
            ),
            {"pid_pattern": f"{CHAT_EVAL_PLACE_PREFIX}%"},
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE :prefix"),
            {"prefix": f"{CHAT_EVAL_USER_PREFIX}%@test.com"},
        )


# ──────────────────────────────────────────────────────────────────────────
#   Sommelier (B2C) DB fixture: 3 restaurants, 10 dishes, 1 user with profile
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class SommelierEvalFixtureScope:
    """Identifiers the Sommelier eval cases reference.

    The catalog is small but spans three neighborhoods and three
    cuisines so cases can exercise filters that the Business fixture
    (single-restaurant) couldn't reach. The synthetic user "Lautaro"
    carries a populated ``UserTasteProfile`` so ``taste_profile``
    awareness, allergy respect, and personalised greeting paths all
    have something to assert against.
    """

    user_id: str
    restaurant_ids: dict[str, str]  # restaurant name → uuid string
    dish_ids: dict[str, str]  # dish name → uuid string
    allergies: list[str]


async def _ensure_category(session, slug: str, name: str) -> int:
    """Get-or-create a Category by slug.

    Categories are seeded into the dev DB; in CI they may not exist.
    The fixture creates them if missing so the eval suite is portable.
    """
    existing = (
        await session.execute(select(Category).where(Category.slug == slug))
    ).scalars().first()
    if existing is not None:
        return existing.id
    cat = Category(slug=slug, name=name)
    session.add(cat)
    await session.flush()
    return cat.id


@pytest.fixture(scope="session")
async def sommelier_eval_scope() -> SommelierEvalFixtureScope:
    """Build a session-scoped catalog the Sommelier evals reuse.

    Three restaurants in three Buenos Aires neighborhoods, ten dishes
    spanning Italian / Japanese / parrilla, and one synthetic user
    "Lautaro" with a populated taste profile (dominant presentation,
    top neighborhoods Palermo, allergies gluten). Committed to the
    dev DB; ``_cleanup_chat_eval_data`` drops everything when the
    session ends.
    """
    async with async_session() as session:
        cat_ids = {
            "italiana": await _ensure_category(session, "italiana", "Italiana"),
            "japonesa": await _ensure_category(session, "japonesa", "Japonesa"),
            "parrilla": await _ensure_category(session, "parrilla", "Parrilla"),
        }

        creator = User(
            id=uuid.uuid4(),
            email=f"{CHAT_EVAL_USER_PREFIX}sommelier_creator@test.com",
            password_hash="x" * 60,
            display_name="Sommelier Creator",
        )
        session.add(creator)
        await session.flush()

        # Three restaurants, distinct neighborhoods + cuisines so cases
        # asking for 'pasta en Palermo' / 'ramen en Belgrano' /
        # 'parrilla en Centro' all have a unique answer.
        restaurants_data = [
            ("Trattoria del Sol", "Palermo", "italiana", -34.59, -58.42),
            ("Yatai Ramen", "Belgrano", "japonesa", -34.56, -58.46),
            ("Asadero del Centro", "Centro", "parrilla", -34.61, -58.38),
        ]
        restaurants: dict[str, Restaurant] = {}
        for r_name, neighborhood, cat_slug, lat, lng in restaurants_data:
            slug_safe = r_name.lower().replace(" ", "-")
            r = Restaurant(
                id=uuid.uuid4(),
                slug=f"pytest-chat-eval-sommelier-{slug_safe}-{uuid.uuid4().hex[:6]}",
                name=r_name,
                location_name=f"{neighborhood}, Buenos Aires",
                city="Buenos Aires",
                google_place_id=(
                    f"{CHAT_EVAL_SOMMELIER_PLACE_PREFIX}"
                    f"{r_name.lower().replace(' ', '_')}"
                ),
                latitude=Decimal(str(lat)),
                longitude=Decimal(str(lng)),
                category_id=cat_ids[cat_slug],
                created_by=creator.id,
            )
            session.add(r)
            restaurants[r_name] = r
        await session.flush()

        # Ten dishes with hand-picked ratings + pillars so cases that
        # filter by ``min_presentation=3`` or ``min_value_prop=3`` have a
        # deterministic subset to land in.
        # Schema: (restaurant, dish, rating, presentation, execution,
        #          value_prop, price_tier).
        dish_specs = [
            ("Trattoria del Sol", "Pasta Carbonara", 4.5, 2, 3, 2, PriceTier.mid),
            ("Trattoria del Sol", "Risotto de Hongos", 4.7, 3, 3, 2, PriceTier.mid),
            ("Trattoria del Sol", "Tiramisú", 4.8, 3, 3, 2, PriceTier.mid),
            ("Trattoria del Sol", "Pizza Margherita", 3.8, 2, 2, 3, PriceTier.low),
            ("Yatai Ramen", "Ramen Tonkotsu", 4.6, 2, 3, 3, PriceTier.mid),
            ("Yatai Ramen", "Sushi Variado", 4.4, 3, 2, 2, PriceTier.high),
            ("Yatai Ramen", "Gyozas", 4.0, 2, 2, 3, PriceTier.low),
            ("Asadero del Centro", "Bife de Chorizo", 4.9, 2, 3, 2, PriceTier.high),
            ("Asadero del Centro", "Provoleta", 4.5, 3, 3, 2, PriceTier.mid),
            ("Asadero del Centro", "Empanadas Caseras", 4.2, 2, 2, 3, PriceTier.low),
        ]
        dishes: dict[str, Dish] = {}
        for r_name, d_name, rating, _pres, _exec, _vp, tier in dish_specs:
            d = Dish(
                id=uuid.uuid4(),
                restaurant_id=restaurants[r_name].id,
                name=d_name,
                computed_rating=Decimal(str(rating)),
                review_count=3,  # matches the seeded reviews below
                price_tier=tier,
                created_by=creator.id,
            )
            session.add(d)
            dishes[d_name] = d
        await session.flush()

        # Three reviews per dish back the computed_rating + EXISTS-based
        # pillar filters in search_dishes. Pillar values match the dish
        # spec so cases with ``min_presentation=3`` resolve to the dishes
        # we expect.
        for r_name, d_name, rating, pres, exec_, vp, _tier in dish_specs:
            for i in range(3):
                rev_email = (
                    f"{CHAT_EVAL_USER_PREFIX}sommelier_reviewer_"
                    f"{d_name.lower().replace(' ', '_')}_{i}@test.com"
                )
                reviewer = User(
                    id=uuid.uuid4(),
                    email=rev_email,
                    password_hash="x" * 60,
                    display_name=f"Eval Reviewer {i}",
                )
                session.add(reviewer)
                await session.flush()
                review = DishReview(
                    id=uuid.uuid4(),
                    dish_id=dishes[d_name].id,
                    user_id=reviewer.id,
                    date_tasted=date(2026, 4, 15),
                    note=(
                        f"Pedimos {d_name} y nos gustó. La cocina cumple "
                        "con lo que promete."
                    ),
                    rating=Decimal(str(rating)),
                    presentation=pres,
                    execution=exec_,
                    value_prop=vp,
                    sentiment_label=SentimentLabel.positive,
                    sentiment_score=Decimal("0.6"),
                    sentiment_analyzed_at=datetime.now(timezone.utc),
                )
                session.add(review)
        await session.flush()

        # Synthetic user "Lautaro" with a populated profile. The Sommelier
        # evals authenticate as this user so the prompt loader injects
        # the Sobre el comensal block.
        lautaro = User(
            id=uuid.uuid4(),
            email=f"{CHAT_EVAL_USER_PREFIX}sommelier_lautaro@test.com",
            password_hash="x" * 60,
            display_name="Lautaro",
        )
        session.add(lautaro)
        await session.flush()

        profile = UserTasteProfile(
            user_id=lautaro.id,
            dominant_pillar=TastePillar.presentation,
            top_neighborhoods=["Palermo"],
            top_categories=["italiana"],
            avg_price_band=PriceBand.mid,
            favorite_tags=["pasta", "postre"],
            preferred_hours=[21],
            allergies=["gluten"],
        )
        session.add(profile)

        await session.commit()

        return SommelierEvalFixtureScope(
            user_id=str(lautaro.id),
            restaurant_ids={n: str(r.id) for n, r in restaurants.items()},
            dish_ids={n: str(d.id) for n, d in dishes.items()},
            allergies=list(profile.allergies),
        )


# ──────────────────────────────────────────────────────────────────────────
#   Dataset loader
# ──────────────────────────────────────────────────────────────────────────


_DATASET_DIR = Path(__file__).parent / "datasets"
_BUSINESS_DATASET = _DATASET_DIR / "business.yaml"
_SOMMELIER_DATASET = _DATASET_DIR / "sommelier.yaml"


def _load_yaml_cases(path: Path) -> list[EvalCase]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [EvalCase.model_validate(entry) for entry in raw]


def load_business_cases() -> list[EvalCase]:
    return _load_yaml_cases(_BUSINESS_DATASET)


def load_sommelier_cases() -> list[EvalCase]:
    return _load_yaml_cases(_SOMMELIER_DATASET)


# ──────────────────────────────────────────────────────────────────────────
#   Per-test session
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def eval_db_session():
    """Open a fresh AsyncSession per test, separate from the loop's own
    sessions. Lets us inspect DB state inside assertions if we want.
    """
    async with async_session() as session:
        yield session


@pytest.fixture
def chat_api_key() -> str | None:
    return _api_key()
