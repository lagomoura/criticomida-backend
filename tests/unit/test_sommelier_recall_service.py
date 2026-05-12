"""Unit tests for the Sommelier review-recall service.

Focus is on the idempotency contract the worker depends on: the
handler must short-circuit gracefully when the diner already reviewed
the dish, when a notification already exists for the same
(user, dish), when the safety guard rejects the delivery, and when
the dish itself disappeared between enqueue and run.

These are pure-logic tests with mocked DB calls. The end-to-end
behaviour (real INSERT + UPDATE under transaction) is covered by
the integration suite where the DB is wired up.
"""

from __future__ import annotations

import uuid as _uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import sommelier_recall_service as svc


# ──────────────────────────────────────────────────────────────────────
#   Test doubles
# ──────────────────────────────────────────────────────────────────────


class _ResultRow:
    """Minimal shape for ``(await db.execute(stmt)).first()``."""

    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _ResultScalarOrNone:
    """Minimal shape for ``(await db.execute(stmt)).scalar_one_or_none()``."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _make_dish_with_restaurant(name: str, restaurant_name: str | None):
    return SimpleNamespace(
        id=_uuid.uuid4(),
        name=name,
        restaurant=(
            SimpleNamespace(id=_uuid.uuid4(), name=restaurant_name)
            if restaurant_name
            else None
        ),
    )


# ──────────────────────────────────────────────────────────────────────
#   _build_recall_text
# ──────────────────────────────────────────────────────────────────────


class TestBuildRecallText:
    async def test_returns_dish_and_restaurant_when_both_present(self):
        dish = _make_dish_with_restaurant("Risotto al funghi", "La Vinoteca")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultScalarOrNone(dish))

        text = await svc._build_recall_text(db, dish_id=dish.id)

        assert text == "Risotto al funghi · La Vinoteca"

    async def test_returns_dish_only_when_restaurant_missing(self):
        # Edge: dish without a linked restaurant (shouldn't happen in
        # prod but the join could come back partial in rare races).
        dish = _make_dish_with_restaurant("Café Turco", None)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultScalarOrNone(dish))

        text = await svc._build_recall_text(db, dish_id=dish.id)

        assert text == "Café Turco"

    async def test_returns_none_when_dish_disappeared(self):
        # Dish deleted between enqueue (24h ago) and run.
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultScalarOrNone(None))

        text = await svc._build_recall_text(db, dish_id=_uuid.uuid4())

        assert text is None


# ──────────────────────────────────────────────────────────────────────
#   process_sommelier_review_recall — idempotency branches
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def user_id():
    return _uuid.uuid4()


@pytest.fixture
def dish_id():
    return _uuid.uuid4()


def _db_returning(*results):
    """Build a DB mock whose ``execute`` cycles through the given
    results in order. Lets a single test assert the exact sequence
    of queries the handler issues before reaching its decision."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()
    return db


class TestProcessRecallSkipsWhenAlreadyReviewed:
    async def test_skip_when_review_exists(self, user_id, dish_id, monkeypatch):
        # First db.execute = review-exists lookup → returns a row.
        db = _db_returning(_ResultRow((_uuid.uuid4(),)))

        spy = AsyncMock()
        monkeypatch.setattr(svc, "should_deliver_notification", spy)

        await svc.process_sommelier_review_recall(
            db, user_id=user_id, dish_id=dish_id
        )

        # The handler must short-circuit BEFORE checking safety or
        # building the text — only the review-exists query runs.
        assert db.execute.await_count == 1
        spy.assert_not_called()
        db.add.assert_not_called()


class TestProcessRecallSkipsWhenNotificationExists:
    async def test_skip_when_notification_already_exists(
        self, user_id, dish_id, monkeypatch
    ):
        db = _db_returning(
            # review-exists query → no row
            _ResultRow(None),
            # notification-exists query → a row
            _ResultRow((_uuid.uuid4(),)),
        )
        spy = AsyncMock()
        monkeypatch.setattr(svc, "should_deliver_notification", spy)

        await svc.process_sommelier_review_recall(
            db, user_id=user_id, dish_id=dish_id
        )

        # Stops before reaching the safety guard.
        spy.assert_not_called()
        db.add.assert_not_called()


class TestProcessRecallSkipsWhenSafetyGuardRejects:
    async def test_skip_when_should_deliver_false(
        self, user_id, dish_id, monkeypatch
    ):
        db = _db_returning(
            _ResultRow(None),  # no review
            _ResultRow(None),  # no prior notif
        )
        monkeypatch.setattr(
            svc, "should_deliver_notification", AsyncMock(return_value=False)
        )

        await svc.process_sommelier_review_recall(
            db, user_id=user_id, dish_id=dish_id
        )

        db.add.assert_not_called()


class TestProcessRecallSkipsWhenDishDisappeared:
    async def test_skip_when_dish_missing_at_run_time(
        self, user_id, dish_id, monkeypatch
    ):
        db = _db_returning(
            _ResultRow(None),  # no review
            _ResultRow(None),  # no prior notif
            _ResultScalarOrNone(None),  # dish lookup returns None
        )
        monkeypatch.setattr(
            svc, "should_deliver_notification", AsyncMock(return_value=True)
        )

        await svc.process_sommelier_review_recall(
            db, user_id=user_id, dish_id=dish_id
        )

        db.add.assert_not_called()


class TestProcessRecallHappyPath:
    async def test_inserts_notification_when_all_checks_pass(
        self, user_id, dish_id, monkeypatch
    ):
        dish = _make_dish_with_restaurant("Pulpo a la gallega", "Sagardi")
        # Pin the dish.id so the assertion below matches the FK we
        # expect the notification row to carry.
        dish.id = dish_id

        db = _db_returning(
            _ResultRow(None),
            _ResultRow(None),
            _ResultScalarOrNone(dish),
        )
        monkeypatch.setattr(
            svc, "should_deliver_notification", AsyncMock(return_value=True)
        )

        await svc.process_sommelier_review_recall(
            db, user_id=user_id, dish_id=dish_id
        )

        db.add.assert_called_once()
        notif = db.add.call_args.args[0]
        assert notif.recipient_user_id == user_id
        assert notif.actor_user_id == svc.SOMMELIER_BOT_USER_ID
        assert notif.kind == "sommelier_review_recall"
        assert notif.target_dish_id == dish_id
        assert notif.text == "Pulpo a la gallega · Sagardi"


# ──────────────────────────────────────────────────────────────────────
#   Bot user id sanity
# ──────────────────────────────────────────────────────────────────────


def test_bot_user_id_matches_migration_seed():
    # If this value ever drifts, the notifications point at a user
    # id that doesn't exist in the DB (FK violation). The migration
    # hard-codes the same UUID; this assert keeps both in lockstep.
    assert str(svc.SOMMELIER_BOT_USER_ID) == (
        "00000000-0000-4000-8000-50616c61746f"
    )
