"""Unit tests for ``update_taste_profile``.

The tool persists user-declared allergies / preferred hours. The unit
surface we pin here is the **anonymous-user error contract**: the
handler must return a guidance payload the agent can read and
*correctly* act on, instead of a bare error that the model can ignore
(which led to "Anoté tus preferencias" leaking out even when the
profile wasn't saved — see prompt regla 5 for the matching rule).

Real DB-backed persistence is exercised by the eval suite where the
fixture user is authenticated.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.services.chat.tools.taste import make_update_taste_profile_tool


class TestAnonymousUserContract:
    async def test_returns_saved_false_with_guidance(self):
        tool = make_update_taste_profile_tool(AsyncMock(), user_id=None)
        result = await tool.handler({"allergies": ["lácteos"]})

        # The agent reads ``saved`` to decide its phrasing. A bare
        # ``error`` field by itself would let the model close the
        # turn with "Anoté…" anyway — the guidance message has to
        # call out which phrases are PROHIBITED so the contract is
        # explicit, not implicit.
        assert result["saved"] is False
        assert result["error"] == "not_authenticated"
        assert "PROHIBIDO" in result["message"]
        assert "anoté" in result["message"].lower()
        # The instruction also has to tell the agent how to recover
        # gracefully — respect the declaration *this turn* without
        # claiming persistence — so the message mentions the session
        # scope explicitly.
        assert "esta conversación" in result["message"]

    async def test_anonymous_does_not_touch_the_db(self):
        db = AsyncMock()
        tool = make_update_taste_profile_tool(db, user_id=None)
        await tool.handler({"allergies": ["gluten"]})
        # Anonymous users are short-circuited BEFORE any DB call.
        db.execute.assert_not_called()


class TestAllergiesMerge:
    """Regression tests for the merge-vs-replace bug: when the
    comensal declares allergies in two separate turns, both have to
    survive. The chat used to only send the latest one and the
    handler would clobber the previous list."""

    async def test_merge_appends_new_allergy_to_existing(self):
        # Stand up a fake DB that returns a profile with one allergy
        # already on file, then captures what the handler writes.
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import uuid as _uuid

        existing_profile = SimpleNamespace(
            allergies=["maní"],
            preferred_hours=[],
        )

        class _Result:
            def scalars(self):
                inner = MagicMock()
                inner.first.return_value = existing_profile
                return inner

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_Result())
        tool = make_update_taste_profile_tool(db, user_id=_uuid.uuid4())

        result = await tool.handler({"allergies": ["nueces"]})
        assert result["saved"] is True
        # The merge keeps maní AND adds nueces.
        assert "maní" in existing_profile.allergies
        assert "nueces" in existing_profile.allergies

    async def test_merge_dedupes_case_insensitive(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import uuid as _uuid

        existing_profile = SimpleNamespace(
            allergies=["Maní"],
            preferred_hours=[],
        )

        class _Result:
            def scalars(self):
                inner = MagicMock()
                inner.first.return_value = existing_profile
                return inner

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_Result())
        tool = make_update_taste_profile_tool(db, user_id=_uuid.uuid4())

        # Same allergy in different casing — the merge must NOT
        # accumulate both spellings.
        await tool.handler({"allergies": ["maní"]})
        assert len(existing_profile.allergies) == 1

    async def test_merge_dedupes_by_synonym_group(self):
        # Production bug: the comensal said "soy alérgico al maní"
        # in turn 1 and "y a las nueces" in turn 2; turn 3 they said
        # "y a la nuez" again. The profile ended up with both
        # ``"nueces"`` and ``"nuez"`` because the dedup compared raw
        # lowercase. ``allergen_canonical_key`` collapses synonym
        # groups — second declaration of the same allergen is now
        # a no-op, regardless of plural/singular/synonym surface.
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import uuid as _uuid

        existing_profile = SimpleNamespace(
            allergies=["nueces"],
            preferred_hours=[],
        )

        class _Result:
            def scalars(self):
                inner = MagicMock()
                inner.first.return_value = existing_profile
                return inner

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_Result())
        tool = make_update_taste_profile_tool(db, user_id=_uuid.uuid4())

        await tool.handler({"allergies": ["nuez"]})
        # Same synonym group → still one entry, original surface kept.
        assert existing_profile.allergies == ["nueces"]


class TestEmptyArgsGuard:
    async def test_empty_args_returns_missing_input_for_authenticated(self):
        # Regression test for a production bug: Gemini Flash Lite
        # emitted ``update_taste_profile`` with ``arguments: "{}"``
        # after the user declared an allergy, then said "registré tu
        # alergia" — the handler used to silently no-op and report
        # ``saved: True``. Now the handler refuses and forces the
        # model to re-emit with the real payload.
        import uuid as _uuid

        tool = make_update_taste_profile_tool(
            AsyncMock(), user_id=_uuid.uuid4()
        )
        result = await tool.handler({})
        assert result["saved"] is False
        assert result["error"] == "missing_input"
        assert "allergies" in result["message"]
        # Anti-mentira clause: the message has to tell the agent NOT
        # to claim a save in the response.
        assert "no le digas" in result["message"].lower()

    async def test_empty_args_short_circuits_before_db(self):
        db = AsyncMock()
        import uuid as _uuid

        tool = make_update_taste_profile_tool(db, user_id=_uuid.uuid4())
        await tool.handler({})
        db.execute.assert_not_called()
