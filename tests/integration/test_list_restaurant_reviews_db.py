"""Integration tests for ``list_restaurant_reviews`` against a real Postgres.

The unit tests in ``tests/unit/test_list_restaurant_reviews_tool.py`` pin
the contract (Pydantic schema, model_validator, range checks, resolver
branches with fakes). These tests exercise the SQL paths the unit tests
can't reach with mocks:

- Real ``func.date()`` comparisons for ``date_from``/``date_to``.
- Real ``NULLS LAST`` semantics for ``most_negative`` / ``most_positive``
  on ``sentiment_score``.
- Real ``Dish.name_normalized`` accent-insensible substring filter
  (depends on the ``public.dish_name_normalized`` computed column).
- Real ``excluded_author_ids_subquery`` against ``user_blocks`` and
  ``user_mutes``.
- Real eager-loaded ``pros_cons`` cap.
- Output enrichment present: ``restaurant.rating``,
  ``restaurant.review_count``, ``would_order_again``, ``meal_period``.

Seed data uses identifiable patterns (``pytest_lrr_*`` users,
``pytest-lrr-*`` slugs, ``pytest_place_lrr_*`` place ids) so the
session-scoped cleanup in ``conftest.py`` and the per-class fixture
here both catch it.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.database import async_session, engine

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


from app.services.chat.tools._resolution import _resolve_restaurant_global
from app.services.chat.tools.search import make_list_restaurant_reviews_tool


# ──────────────────────────────────────────────────────────────────────────
#   Seed helpers — raw SQL inserts so tests stay fast and isolated.
# ──────────────────────────────────────────────────────────────────────────


SEED_TAG = "lrr"  # list_restaurant_reviews — narrow cleanup namespace


async def _insert_user(*, suffix: str | None = None) -> str:
    """Create a throwaway user. Email pattern matches conftest cleanup."""
    uid = str(uuid.uuid4())
    suffix = suffix or uuid.uuid4().hex[:8]
    email = f"pytest_{SEED_TAG}_{suffix}@test.com"
    handle = f"pytest_{SEED_TAG}_{suffix}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, handle, display_name, "
                " role, created_at, updated_at) "
                "VALUES (:id, :email, 'fake-hash', :handle, :name, "
                "        CAST('user' AS user_role), now(), now())"
            ),
            {
                "id": uid,
                "email": email,
                "handle": handle,
                "name": handle,
            },
        )
    return uid


async def _insert_restaurant(
    *,
    name: str,
    slug: str | None = None,
    location: str = "Palermo",
    city: str = "Buenos Aires",
    rating: float = 4.1,
    review_count: int = 0,
    created_by: str,
) -> str:
    rid = str(uuid.uuid4())
    slug = slug or f"pytest-{SEED_TAG}-{uuid.uuid4().hex[:8]}"
    place_id = f"pytest_place_{SEED_TAG}_{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO restaurants "
                "(id, slug, name, location_name, city, latitude, longitude, "
                " google_place_id, computed_rating, review_count, "
                " created_by, created_at, updated_at) "
                "VALUES (:id, :slug, :name, :loc, :city, -34.6, -58.4, "
                "        :pid, :rating, :rc, :user, now(), now())"
            ),
            {
                "id": rid,
                "slug": slug,
                "name": name,
                "loc": location,
                "city": city,
                "pid": place_id,
                "rating": rating,
                "rc": review_count,
                "user": created_by,
            },
        )
    return rid


async def _insert_dish(
    restaurant_id: str, name: str, *, created_by: str
) -> str:
    did = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO dishes "
                "(id, restaurant_id, name, computed_rating, review_count, "
                " created_by, created_at) "
                "VALUES (:id, :rid, :name, 0, 0, :user, now())"
            ),
            {
                "id": did,
                "rid": restaurant_id,
                "name": name,
                "user": created_by,
            },
        )
    return did


async def _insert_review(
    dish_id: str,
    *,
    user_id: str,
    rating: float = 4.0,
    note: str = "Reseña de prueba pytest.",
    sentiment_label: str | None = None,
    sentiment_score: float | None = None,
    presentation: int | None = None,
    execution: int | None = None,
    value_prop: int | None = None,
    would_order_again: bool | None = None,
    meal_period: str | None = None,
    days_ago: int = 0,
) -> str:
    """Insert a DishReview with controlled fields. ``days_ago`` shifts
    ``created_at`` backwards so date-range tests can pin the timeline."""
    rid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO dish_reviews "
                "(id, dish_id, user_id, date_tasted, meal_period, note, "
                " rating, presentation, execution, value_prop, "
                " would_order_again, sentiment_label, sentiment_score, "
                " is_anonymous, created_at, updated_at) "
                "VALUES (:id, :did, :uid, current_date, "
                "        CAST(:meal AS meal_period), :note, :rating, "
                "        :pres, :exec, :val, :wouldorder, "
                "        CAST(:slabel AS sentiment_label), :sscore, "
                "        false, :ts, :ts)"
            ),
            {
                "id": rid,
                "did": dish_id,
                "uid": user_id,
                "meal": meal_period,
                "note": note,
                "rating": rating,
                "pres": presentation,
                "exec": execution,
                "val": value_prop,
                "wouldorder": would_order_again,
                "slabel": sentiment_label,
                "sscore": sentiment_score,
                "ts": created_at,
            },
        )
    return rid


async def _insert_pros_cons(
    review_id: str, *, pros: list[str], cons: list[str]
) -> None:
    if not pros and not cons:
        return
    async with engine.begin() as conn:
        for pro in pros:
            await conn.execute(
                text(
                    "INSERT INTO dish_review_pros_cons "
                    "(dish_review_id, type, text) "
                    "VALUES (:rid, CAST(:type AS dish_review_pros_cons_type), :text)"
                ),
                {"rid": review_id, "type": "pro", "text": pro},
            )
        for con in cons:
            await conn.execute(
                text(
                    "INSERT INTO dish_review_pros_cons "
                    "(dish_review_id, type, text) "
                    "VALUES (:rid, CAST(:type AS dish_review_pros_cons_type), :text)"
                ),
                {"rid": review_id, "type": "con", "text": con},
            )


async def _block(blocker_id: str, blocked_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_blocks (blocker_id, blocked_id, created_at) "
                "VALUES (:b, :t, now()) ON CONFLICT DO NOTHING"
            ),
            {"b": blocker_id, "t": blocked_id},
        )


async def _mute(muter_id: str, muted_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_mutes (muter_id, muted_id, created_at) "
                "VALUES (:m, :t, now()) ON CONFLICT DO NOTHING"
            ),
            {"m": muter_id, "t": muted_id},
        )


@pytest.fixture
async def cleanup_lrr_data():
    """Per-test cleanup. Order respects FK: reviews/dishes cascade via
    restaurants → cascade via users. Blocks/mutes cascade via users.

    We delete by the SEED_TAG namespace (slug + email pattern) so a
    parallel test using different patterns is unaffected.
    """
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM restaurants "
                "WHERE slug LIKE :slug "
                "   OR google_place_id LIKE :pid "
                "   OR created_by IN (SELECT id FROM users WHERE email LIKE :email)"
            ),
            {
                "slug": f"pytest-{SEED_TAG}-%",
                "pid": f"pytest_place_{SEED_TAG}_%",
                "email": f"pytest_{SEED_TAG}_%@test.com",
            },
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE :email"),
            {"email": f"pytest_{SEED_TAG}_%@test.com"},
        )


# ──────────────────────────────────────────────────────────────────────────
#   _resolve_restaurant_global — DB-backed branches
# ──────────────────────────────────────────────────────────────────────────


class TestResolverDB:
    @pytest.mark.asyncio
    async def test_resolves_by_uuid(self, cleanup_lrr_data):
        author = await _insert_user(suffix="resolveruuid")
        rid = await _insert_restaurant(name="Resolver UUID Resto", created_by=author)
        async with async_session() as db:
            rest, hint = await _resolve_restaurant_global(
                db,
                restaurant_id=rid,
                restaurant_slug=None,
                restaurant_name=None,
            )
            assert hint is None
            assert rest is not None
            assert str(rest.id) == rid

    @pytest.mark.asyncio
    async def test_resolves_by_slug(self, cleanup_lrr_data):
        author = await _insert_user(suffix="resolverslug")
        slug = f"pytest-{SEED_TAG}-slug-{uuid.uuid4().hex[:6]}"
        await _insert_restaurant(
            name="Resolver Slug Resto", slug=slug, created_by=author
        )
        async with async_session() as db:
            rest, hint = await _resolve_restaurant_global(
                db,
                restaurant_id=None,
                restaurant_slug=slug,
                restaurant_name=None,
            )
            assert hint is None
            assert rest is not None
            assert rest.slug == slug

    @pytest.mark.asyncio
    async def test_resolves_by_unique_name(self, cleanup_lrr_data):
        author = await _insert_user(suffix="resolvername")
        # Use a randomized unique name so other tests don't collide.
        unique_name = f"Pytest LRR Unique {uuid.uuid4().hex[:8]}"
        await _insert_restaurant(name=unique_name, created_by=author)
        async with async_session() as db:
            rest, hint = await _resolve_restaurant_global(
                db,
                restaurant_id=None,
                restaurant_slug=None,
                restaurant_name=unique_name,
            )
            assert hint is None
            assert rest is not None
            assert rest.name == unique_name

    @pytest.mark.asyncio
    async def test_accent_insensible_name_match(self, cleanup_lrr_data):
        # Real Postgres ILIKE is case-insensitive but NOT accent-insensitive;
        # the resolver's Python post-filter strips accents so "Cafe Pytest"
        # should match a name with "Café Pytest" and vice-versa.
        author = await _insert_user(suffix="accent")
        seed = f"Café Pytest {uuid.uuid4().hex[:8]}"
        await _insert_restaurant(name=seed, created_by=author)
        # Search without accent — should still resolve.
        needle = seed.replace("Café", "Cafe")
        async with async_session() as db:
            rest, hint = await _resolve_restaurant_global(
                db,
                restaurant_id=None,
                restaurant_slug=None,
                restaurant_name=needle,
            )
            assert hint is None
            assert rest is not None
            assert rest.name == seed

    @pytest.mark.asyncio
    async def test_ambiguous_name_returns_disambiguation(self, cleanup_lrr_data):
        author = await _insert_user(suffix="ambig")
        token = uuid.uuid4().hex[:8]
        await _insert_restaurant(
            name=f"Pytest Ambig {token} Palermo",
            location="Palermo",
            created_by=author,
        )
        await _insert_restaurant(
            name=f"Pytest Ambig {token} Belgrano",
            location="Belgrano",
            created_by=author,
        )
        async with async_session() as db:
            rest, hint = await _resolve_restaurant_global(
                db,
                restaurant_id=None,
                restaurant_slug=None,
                restaurant_name=f"Pytest Ambig {token}",
            )
            assert rest is None
            assert hint["needs_disambiguation"] is True
            assert len(hint["candidates"]) == 2
            barrios = {c["location_name"] for c in hint["candidates"]}
            assert barrios == {"Palermo", "Belgrano"}

    @pytest.mark.asyncio
    async def test_no_match_returns_no_match(self, cleanup_lrr_data):
        # No restaurants with this needle in DB — resolver returns no_match.
        needle = f"Inexistente LRR {uuid.uuid4().hex[:8]}"
        async with async_session() as db:
            rest, hint = await _resolve_restaurant_global(
                db,
                restaurant_id=None,
                restaurant_slug=None,
                restaurant_name=needle,
            )
            assert rest is None
            assert hint["error"] == "no_match"
            assert hint["query"] == needle


# ──────────────────────────────────────────────────────────────────────────
#   Query behavior — sentiment / sort / filters against real SQL
# ──────────────────────────────────────────────────────────────────────────


class TestQueryDB:
    @pytest.mark.asyncio
    async def test_sort_most_negative_orders_by_sentiment_score_asc(
        self, cleanup_lrr_data
    ):
        author = await _insert_user(suffix="sortneg")
        rid = await _insert_restaurant(name="Sort Neg Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Neg", created_by=author)
        # Mix of sentiment_scores. most_negative should sort ASC NULLS LAST.
        await _insert_review(
            dish, user_id=author, rating=2.0,
            sentiment_label="negative", sentiment_score=-0.9,
            note="muy malo",
        )
        await _insert_review(
            dish, user_id=author, rating=3.0,
            sentiment_label="negative", sentiment_score=-0.5,
            note="medio malo",
        )
        await _insert_review(
            dish, user_id=author, rating=2.5,
            sentiment_label=None, sentiment_score=None,
            note="sin sentiment analizado",
        )

        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler(
                {
                    "restaurant_id": rid,
                    "sort": "most_negative",
                    "limit": 10,
                }
            )
        scores = [r["sentiment_score"] for r in result["reviews"]]
        # Two non-null negatives sorted ASC, then the NULL.
        assert scores[:2] == sorted([s for s in scores if s is not None])
        assert scores[-1] is None

    @pytest.mark.asyncio
    async def test_sentiment_negative_filter_excludes_others(
        self, cleanup_lrr_data
    ):
        author = await _insert_user(suffix="sentfilter")
        rid = await _insert_restaurant(name="Sent Filter Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Filter", created_by=author)
        await _insert_review(
            dish, user_id=author, sentiment_label="positive", sentiment_score=0.8
        )
        await _insert_review(
            dish, user_id=author, sentiment_label="neutral", sentiment_score=0.1
        )
        await _insert_review(
            dish, user_id=author, sentiment_label="negative", sentiment_score=-0.7
        )
        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler(
                {"restaurant_id": rid, "sentiment": "negative"}
            )
        labels = {r["sentiment_label"] for r in result["reviews"]}
        assert labels == {"negative"}
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_dish_name_contains_accent_insensible(self, cleanup_lrr_data):
        author = await _insert_user(suffix="dishname")
        rid = await _insert_restaurant(name="DishName Resto", created_by=author)
        risotto = await _insert_dish(rid, "Risotto de hongos", created_by=author)
        pasta = await _insert_dish(rid, "Pasta carbonara", created_by=author)
        await _insert_review(risotto, user_id=author, note="risotto rico")
        await _insert_review(pasta, user_id=author, note="pasta rica")

        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            # "RISOTTO" uppercase + no accents — name_normalized handles it.
            result = await tool.handler(
                {"restaurant_id": rid, "dish_name_contains": "RISOTTO"}
            )
        names = {r["dish_name"] for r in result["reviews"]}
        assert names == {"Risotto de hongos"}

    @pytest.mark.asyncio
    async def test_date_range_filters_inclusive(self, cleanup_lrr_data):
        author = await _insert_user(suffix="daterange")
        rid = await _insert_restaurant(name="DateRange Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Date", created_by=author)
        # Reviews 50 / 25 / 5 days ago. Window 30..10 days should keep only
        # the 25-day-old one.
        await _insert_review(
            dish, user_id=author, days_ago=50, note="hace mucho"
        )
        await _insert_review(
            dish, user_id=author, days_ago=25, note="dentro del rango"
        )
        await _insert_review(
            dish, user_id=author, days_ago=5, note="reciente"
        )

        date_to = (datetime.now(timezone.utc) - timedelta(days=10)).date()
        date_from = (datetime.now(timezone.utc) - timedelta(days=30)).date()
        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler(
                {
                    "restaurant_id": rid,
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                }
            )
        notes = {r["excerpt"] for r in result["reviews"]}
        assert notes == {"dentro del rango"}

    @pytest.mark.asyncio
    async def test_rating_range_inclusive_bounds(self, cleanup_lrr_data):
        author = await _insert_user(suffix="rating")
        rid = await _insert_restaurant(name="Rating Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Rating", created_by=author)
        await _insert_review(dish, user_id=author, rating=1.5)
        await _insert_review(dish, user_id=author, rating=2.5)
        await _insert_review(dish, user_id=author, rating=4.0)
        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler(
                {
                    "restaurant_id": rid,
                    "min_rating": 2.0,
                    "max_rating": 3.0,
                }
            )
        ratings = sorted(r["rating"] for r in result["reviews"])
        assert ratings == [2.5]


# ──────────────────────────────────────────────────────────────────────────
#   Safety filter — bidirectional block + mute
# ──────────────────────────────────────────────────────────────────────────


class TestSafetyDB:
    @pytest.mark.asyncio
    async def test_blocked_author_excluded_for_viewer(self, cleanup_lrr_data):
        author = await _insert_user(suffix="bauthor")
        blocked = await _insert_user(suffix="bblocked")
        viewer = await _insert_user(suffix="bviewer")
        rid = await _insert_restaurant(name="Block Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Block", created_by=author)
        # Two reviews: one by author (visible), one by blocked (hidden).
        await _insert_review(dish, user_id=author, note="visible")
        await _insert_review(dish, user_id=blocked, note="bloqueado")
        await _block(viewer, blocked)

        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(
                db, user_id=uuid.UUID(viewer)
            )
            result = await tool.handler({"restaurant_id": rid})
        excerpts = {r["excerpt"] for r in result["reviews"]}
        assert excerpts == {"visible"}

    @pytest.mark.asyncio
    async def test_blocker_of_viewer_also_excluded(self, cleanup_lrr_data):
        # Bidirectional: if author blocked the viewer, viewer also doesn't
        # see them. ``excluded_author_ids_subquery`` UNIONs both directions.
        author = await _insert_user(suffix="bidir1")
        blocker = await _insert_user(suffix="bidir2")
        viewer = await _insert_user(suffix="bidir3")
        rid = await _insert_restaurant(name="Bidir Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Bidir", created_by=author)
        await _insert_review(dish, user_id=author, note="visible")
        await _insert_review(dish, user_id=blocker, note="oculto")
        await _block(blocker, viewer)  # blocker blocks viewer

        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(
                db, user_id=uuid.UUID(viewer)
            )
            result = await tool.handler({"restaurant_id": rid})
        excerpts = {r["excerpt"] for r in result["reviews"]}
        assert excerpts == {"visible"}

    @pytest.mark.asyncio
    async def test_muted_author_excluded(self, cleanup_lrr_data):
        author = await _insert_user(suffix="mauthor")
        muted = await _insert_user(suffix="mmuted")
        viewer = await _insert_user(suffix="mviewer")
        rid = await _insert_restaurant(name="Mute Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Mute", created_by=author)
        await _insert_review(dish, user_id=author, note="visible")
        await _insert_review(dish, user_id=muted, note="muteado")
        await _mute(viewer, muted)

        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(
                db, user_id=uuid.UUID(viewer)
            )
            result = await tool.handler({"restaurant_id": rid})
        excerpts = {r["excerpt"] for r in result["reviews"]}
        assert excerpts == {"visible"}

    @pytest.mark.asyncio
    async def test_anonymous_viewer_sees_everything(self, cleanup_lrr_data):
        author = await _insert_user(suffix="anonauth")
        other = await _insert_user(suffix="anonother")
        viewer = await _insert_user(suffix="anonviewer")
        rid = await _insert_restaurant(name="Anon Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Anon", created_by=author)
        await _insert_review(dish, user_id=author, note="A")
        await _insert_review(dish, user_id=other, note="B")
        # Even if viewer has blocks against other, anonymous tool ignores them.
        await _block(viewer, other)

        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler({"restaurant_id": rid})
        excerpts = {r["excerpt"] for r in result["reviews"]}
        assert excerpts == {"A", "B"}


# ──────────────────────────────────────────────────────────────────────────
#   Output shape — anonymization, excerpt, pros/cons, enrichment
# ──────────────────────────────────────────────────────────────────────────


class TestOutputShapeDB:
    @pytest.mark.asyncio
    async def test_review_items_omit_user_identity(self, cleanup_lrr_data):
        author = await _insert_user(suffix="shape1")
        rid = await _insert_restaurant(name="Shape Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Shape", created_by=author)
        await _insert_review(dish, user_id=author)
        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler({"restaurant_id": rid})
        for item in result["reviews"]:
            # No identity / business-only fields exposed to the comensal.
            assert "user_id" not in item
            assert "author" not in item
            assert "display_name" not in item
            assert "has_owner_response" not in item

    @pytest.mark.asyncio
    async def test_excerpt_truncates_and_collapses_whitespace(
        self, cleanup_lrr_data
    ):
        author = await _insert_user(suffix="shape2")
        rid = await _insert_restaurant(name="Excerpt Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Excerpt", created_by=author)
        long_note = "x" * 500 + "\n\n  doble espacio  \n\t tab"
        await _insert_review(dish, user_id=author, note=long_note)
        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler({"restaurant_id": rid})
        excerpt = result["reviews"][0]["excerpt"]
        assert len(excerpt) <= 240
        assert "\n" not in excerpt
        assert "\t" not in excerpt
        assert "  " not in excerpt  # no double spaces

    @pytest.mark.asyncio
    async def test_pros_cons_cap_three_each(self, cleanup_lrr_data):
        author = await _insert_user(suffix="shape3")
        rid = await _insert_restaurant(name="ProsCons Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato ProsCons", created_by=author)
        review_id = await _insert_review(dish, user_id=author)
        await _insert_pros_cons(
            review_id,
            pros=[f"pro {i}" for i in range(5)],
            cons=[f"con {i}" for i in range(5)],
        )
        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler({"restaurant_id": rid})
        item = result["reviews"][0]
        assert len(item["pros"]) == 3
        assert len(item["cons"]) == 3

    @pytest.mark.asyncio
    async def test_restaurant_enrichment_and_per_review_fields(
        self, cleanup_lrr_data
    ):
        author = await _insert_user(suffix="shape4")
        rid = await _insert_restaurant(
            name="Enrich Resto",
            rating=4.2,
            review_count=12,
            created_by=author,
        )
        dish = await _insert_dish(rid, "Plato Enrich", created_by=author)
        await _insert_review(
            dish,
            user_id=author,
            would_order_again=False,
            meal_period="dinner",
            presentation=2,
            execution=1,
            value_prop=1,
        )
        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler({"restaurant_id": rid})
        # Restaurant enrichment for editorial framing.
        assert result["restaurant"]["rating"] == 4.2
        assert result["restaurant"]["review_count"] == 12
        # Per-review enrichment.
        item = result["reviews"][0]
        assert item["would_order_again"] is False
        assert item["meal_period"] == "dinner"
        assert item["presentation"] == 2
        assert item["execution"] == 1
        assert item["value_prop"] == 1

    @pytest.mark.asyncio
    async def test_applied_filters_echoes_inputs(self, cleanup_lrr_data):
        author = await _insert_user(suffix="shape5")
        rid = await _insert_restaurant(name="Echo Resto", created_by=author)
        dish = await _insert_dish(rid, "Plato Echo", created_by=author)
        await _insert_review(
            dish, user_id=author, sentiment_label="negative", sentiment_score=-0.6
        )
        async with async_session() as db:
            tool = make_list_restaurant_reviews_tool(db, user_id=None)
            result = await tool.handler(
                {
                    "restaurant_id": rid,
                    "sentiment": "negative",
                    "sort": "most_negative",
                    "limit": 5,
                    "min_rating": 1.0,
                    "max_rating": 5.0,
                    "dish_name_contains": "echo",
                }
            )
        applied = result["applied_filters"]
        assert applied["sentiment"] == "negative"
        assert applied["sort"] == "most_negative"
        assert applied["limit"] == 5
        assert applied["min_rating"] == 1.0
        assert applied["max_rating"] == 5.0
        assert applied["dish_name_contains"] == "echo"
