"""Unit tests for the batch path of ``backfill_sentiment``.

The script itself is mostly orchestration around Gemini's Batch API and
DB persistence. We test the critical correctness piece — mapping the
batch's inlined responses back to the input snapshots in lockstep —
without touching the DB or hitting the API.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.dish import SentimentLabel
from app.scripts import backfill_sentiment as bf
from app.services.sentiment_service import SentimentResult, _SentimentSchema


def _snap(note: str = "buena pizza", rating: float | None = 4.0) -> bf._ReviewSnapshot:
    return bf._ReviewSnapshot(id=uuid.uuid4(), note=note, rating=rating)


def _mock_response(label: str, score: float) -> MagicMock:
    """Build a ``GenerateContentResponse`` mock whose ``.parsed`` returns
    a real ``_SentimentSchema`` (so ``parse_sentiment_response`` accepts
    it)."""
    response = MagicMock()
    response.parsed = _SentimentSchema(label=label, score=score)
    return response


def _inlined(response: MagicMock | None = None, error: object | None = None) -> MagicMock:
    item = MagicMock()
    item.response = response
    item.error = error
    return item


def test_build_inlined_requests_one_per_snapshot():
    snaps = [_snap("rico"), _snap("frío", rating=2.0), _snap("ok", rating=None)]
    reqs = bf._build_inlined_requests(snaps)
    assert len(reqs) == 3
    for snap, req in zip(snaps, reqs):
        assert req.model == bf.SENTIMENT_MODEL
        # The user prompt should embed the note verbatim — that's how we
        # confirm the batch lines up with what the sync path would have
        # sent for the same review.
        assert snap.note in req.contents
        assert req.config is not None


@pytest.mark.asyncio
async def test_consume_batch_persists_one_row_per_response(monkeypatch):
    snaps = [_snap("rico"), _snap("frío"), _snap("ok")]
    batch = MagicMock()
    batch.dest.inlined_responses = [
        _inlined(_mock_response("positive", 0.7)),
        _inlined(_mock_response("negative", -0.6)),
        _inlined(_mock_response("neutral", 0.05)),
    ]

    persisted: list[tuple[uuid.UUID, SentimentResult]] = []

    async def fake_persist(review_id, result):
        persisted.append((review_id, result))
        return True

    monkeypatch.setattr(bf, "_persist_result", fake_persist)
    written = await bf._consume_batch(batch, snaps)

    assert written == 3
    assert [p[0] for p in persisted] == [s.id for s in snaps]
    assert [p[1].label for p in persisted] == [
        SentimentLabel.positive,
        SentimentLabel.negative,
        SentimentLabel.neutral,
    ]


@pytest.mark.asyncio
async def test_consume_batch_skips_errored_entries(monkeypatch):
    snaps = [_snap("rico"), _snap("frío")]
    batch = MagicMock()
    batch.dest.inlined_responses = [
        _inlined(_mock_response("positive", 0.8)),
        _inlined(response=None, error=MagicMock(message="quota exceeded")),
    ]

    persisted = []

    async def fake_persist(review_id, result):
        persisted.append((review_id, result))
        return True

    monkeypatch.setattr(bf, "_persist_result", fake_persist)
    written = await bf._consume_batch(batch, snaps)

    assert written == 1
    assert persisted[0][0] == snaps[0].id


@pytest.mark.asyncio
async def test_consume_batch_refuses_to_persist_on_length_mismatch(monkeypatch):
    """If Gemini returns fewer responses than requests (PARTIALLY_SUCCEEDED
    can do this), persisting by position would mis-label reviews. Bail."""
    snaps = [_snap("a"), _snap("b"), _snap("c")]
    batch = MagicMock()
    batch.dest.inlined_responses = [_inlined(_mock_response("positive", 0.5))]
    batch.name = "batches/test-123"

    fake_persist = AsyncMock(return_value=True)
    monkeypatch.setattr(bf, "_persist_result", fake_persist)

    written = await bf._consume_batch(batch, snaps)

    assert written == 0
    fake_persist.assert_not_called()


@pytest.mark.asyncio
async def test_consume_batch_handles_missing_dest(monkeypatch):
    snaps = [_snap("a")]
    batch = MagicMock()
    batch.dest = None
    batch.name = "batches/empty"

    fake_persist = AsyncMock(return_value=True)
    monkeypatch.setattr(bf, "_persist_result", fake_persist)

    written = await bf._consume_batch(batch, snaps)
    assert written == 0
    fake_persist.assert_not_called()
