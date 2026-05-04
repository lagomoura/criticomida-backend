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
from sqlalchemy import text

from app.database import async_session, engine
from app.models.dish import Dish, DishReview, SentimentLabel
from app.models.owner_content import DishReviewOwnerResponse
from app.models.restaurant import Restaurant
from app.models.user import User
from tests.chat.evals.runner import EvalCase


CHAT_EVAL_PLACE_ID = "pytest_chat_eval_main"
CHAT_EVAL_USER_PREFIX = "pytest_chat_eval_"


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

    Order: restaurants first (cascades dishes → reviews → owner_responses),
    then users (cascades anything else FK-bound to them).
    """
    yield
    if not _evals_enabled():
        return
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM restaurants WHERE google_place_id = :pid"
            ),
            {"pid": CHAT_EVAL_PLACE_ID},
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE :prefix"),
            {"prefix": f"{CHAT_EVAL_USER_PREFIX}%@test.com"},
        )


# ──────────────────────────────────────────────────────────────────────────
#   Dataset loader
# ──────────────────────────────────────────────────────────────────────────


_DATASET_PATH = Path(__file__).parent / "datasets" / "business.yaml"


def load_business_cases() -> list[EvalCase]:
    if not _DATASET_PATH.exists():
        return []
    raw = yaml.safe_load(_DATASET_PATH.read_text(encoding="utf-8")) or []
    return [EvalCase.model_validate(entry) for entry in raw]


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
