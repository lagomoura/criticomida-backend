"""Pytest entrypoint for the Business chat eval suite.

Each YAML case becomes a parametrized test. Skip-by-default unless
``RUN_CHAT_EVALS=1`` is set (see conftest.py for the gate).

Run with:

    RUN_CHAT_EVALS=1 CHAT_API_KEY=$ANTHROPIC_KEY \\
        pytest tests/chat/evals/test_business_evals.py -v
"""

from __future__ import annotations

import pytest

from app.models.chat import ChatAgent
from tests.chat.evals.conftest import EvalFixtureScope, load_business_cases
from tests.chat.evals.runner import EvalCase, run_eval_case


_CASES = load_business_cases()


@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=[c.id for c in _CASES],
)
async def test_business_eval_case(
    case: EvalCase,
    chat_eval_scope: EvalFixtureScope,
    eval_db_session,
    chat_api_key: str | None,
) -> None:
    import uuid as _uuid

    result = await run_eval_case(
        case,
        db=eval_db_session,
        agent=ChatAgent.business,
        restaurant_scope_id=chat_eval_scope.restaurant_id,
        user_id=_uuid.UUID(chat_eval_scope.owner_user_id),
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
