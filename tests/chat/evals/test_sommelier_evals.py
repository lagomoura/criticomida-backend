"""Pytest entrypoint for the Sommelier chat eval suite.

Mirror of ``test_business_evals.py`` for the B2C agent. Each YAML case
becomes a parametrized test; skip-by-default unless ``RUN_CHAT_EVALS=1``
is set (see conftest.py for the gate).

Differences from the Business suite:

- ``restaurant_scope_id=None`` — Sommelier sees the whole catalog. The
  fixture seeds three restaurants in three neighborhoods so cases can
  exercise the global resolver, the multi-restaurant ranking, and the
  "where" decoder ("una pasta en Palermo").
- The user id is "Lautaro" — a synthetic comensal with a populated
  ``UserTasteProfile`` (dominant presentation, top neighborhoods
  Palermo, allergies gluten). The prompt loader injects the
  ``Sobre el comensal`` block so taste-awareness cases have something
  to assert against.
- The model defaults to ``default_b2c_model()``. Today this is the same
  Gemini 3.1 Flash Lite preview as the Business; we pass it explicitly
  so a future Business override (``CHAT_MODEL_B2B``) doesn't accidentally
  flip the Sommelier suite.

Run with:

    RUN_CHAT_EVALS=1 CHAT_API_KEY=$ANTHROPIC_KEY \\
        pytest tests/chat/evals/test_sommelier_evals.py -v
"""

from __future__ import annotations

import uuid as _uuid

import pytest

from app.models.chat import ChatAgent
from app.services.chat.agent_loop import default_b2c_model
from tests.chat.evals.conftest import (
    SommelierEvalFixtureScope,
    load_sommelier_cases,
)
from tests.chat.evals.runner import EvalCase, run_eval_case


_CASES = load_sommelier_cases()


@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=[c.id for c in _CASES],
)
async def test_sommelier_eval_case(
    case: EvalCase,
    sommelier_eval_scope: SommelierEvalFixtureScope,
    eval_db_session,
    chat_api_key: str | None,
) -> None:
    # ``case.anonymous`` overrides the default user binding so we can
    # exercise tool branches that only trigger without auth — e.g.
    # ``update_taste_profile`` returning ``saved: false`` for an
    # anonymous comensal, which used to slip through with a logged-in
    # fixture even though the bug only manifests anonymously.
    user_id_override = (
        None
        if case.anonymous
        else _uuid.UUID(sommelier_eval_scope.user_id)
    )
    result = await run_eval_case(
        case,
        db=eval_db_session,
        agent=ChatAgent.sommelier,
        restaurant_scope_id=None,
        user_id=user_id_override,
        model=default_b2c_model(),
        api_key=chat_api_key,
    )
    if not result.passed:
        pytest.fail(
            f"\n[{case.id}] FAILED\n"
            f"input: {case.user_input!r}\n"
            f"final text: {result.final_text!r}\n"
            f"tool calls: {[(tc.name, tc.args) for tc in result.tool_calls]}\n"
            f"failures:\n  - " + "\n  - ".join(result.failures)
        )
