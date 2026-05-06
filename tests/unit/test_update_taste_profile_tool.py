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
