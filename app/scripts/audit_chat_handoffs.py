"""Detect chat turns where the LLM "gave up" instead of acting.

The smell: an assistant turn that ends without calling any tool AND
contains hand-back phrases ("necesito que me digas...", "indicame el
ID...", "qué nombre exacto...", etc.). When the user is supposed to
get an answer and instead receives a request for technical data, the
agent has failed at its job.

This script is the **third layer of defense** under the prompt rule
and the defensive tool contract:

1. Prompt — Regla #0 in each agent's system prompt.
2. Tool contract — tools resolve fuzzy inputs internally instead of
   requiring IDs.
3. This audit — surfaces production failures so they can be patched.

Run on the live DB to get a count + samples of suspect turns:

    python -m app.scripts.audit_chat_handoffs --since 7d
    python -m app.scripts.audit_chat_handoffs --agent business --limit 20

Exit code: 0 if no suspect turns above threshold, 1 otherwise. Wire
into a daily cron and alert when the rate climbs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select

from app.database import async_session
from app.models.chat import ChatAgent, ChatConversation, ChatMessage


logger = logging.getLogger("audit_chat_handoffs")


# Phrases that indicate the agent is asking the user for technical
# data instead of resolving on its own. Lower-case, substring match.
_HANDBACK_PATTERNS = [
    r"necesito (que me|saber|conocer)",
    r"indicame (el|cuál|qué)",
    r"podrías (decirme|indicarme|darme)",
    r"(el|un|qué) id\b",
    r"(el|un) uuid",
    r"(el )?nombre exacto",
    r"i need (the|to know|you to)",
    r"could you (tell|provide|give) me",
    r"please (tell|provide|share)",
    r"what is the (id|uuid)",
]
_HANDBACK_RE = re.compile("|".join(_HANDBACK_PATTERNS), re.IGNORECASE)


def _is_handback(content: str) -> str | None:
    """Return the matched phrase if the content looks like a hand-back,
    else None."""
    if not content:
        return None
    m = _HANDBACK_RE.search(content)
    return m.group(0) if m else None


def _parse_since(spec: str) -> datetime:
    """Accept '7d', '24h', or an ISO timestamp."""
    now = datetime.now(timezone.utc)
    if spec.endswith("d"):
        return now - timedelta(days=int(spec[:-1]))
    if spec.endswith("h"):
        return now - timedelta(hours=int(spec[:-1]))
    return datetime.fromisoformat(spec)


async def _audit(
    *,
    since: datetime,
    agent: ChatAgent | None,
    limit: int,
) -> tuple[int, int, list[dict]]:
    async with async_session() as db:
        conditions = [
            ChatMessage.role == "assistant",
            ChatMessage.created_at >= since,
        ]
        stmt = (
            select(
                ChatMessage.id,
                ChatMessage.content,
                ChatMessage.tool_calls,
                ChatMessage.created_at,
                ChatConversation.agent,
                ChatConversation.id.label("conversation_id"),
            )
            .join(
                ChatConversation,
                ChatConversation.id == ChatMessage.conversation_id,
            )
            .where(and_(*conditions))
            .order_by(ChatMessage.created_at.desc())
        )
        if agent is not None:
            stmt = stmt.where(ChatConversation.agent == agent)

        rows = list((await db.execute(stmt)).all())

    total = len(rows)
    suspects: list[dict] = []
    for r in rows:
        # tool_calls is a JSONB list; treat empty list / null as "no tool
        # call". A single thinking block with no actual tool counts as
        # a hand-back too.
        had_tool = isinstance(r.tool_calls, list) and any(
            (tc or {}).get("name") for tc in r.tool_calls
        )
        if had_tool:
            continue
        match = _is_handback(r.content or "")
        if not match:
            continue
        suspects.append(
            {
                "message_id": str(r.id),
                "conversation_id": str(r.conversation_id),
                "agent": r.agent.value if hasattr(r.agent, "value") else str(r.agent),
                "created_at": r.created_at.isoformat(),
                "matched_phrase": match,
                "content_preview": (r.content or "")[:200],
            }
        )

    suspects = suspects[:limit]
    return total, len(suspects), suspects


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default="7d",
        help="Lookback window: '7d', '24h', or ISO timestamp. Default 7d.",
    )
    parser.add_argument(
        "--agent",
        choices=[a.value for a in ChatAgent],
        default=None,
        help="Filter by agent. Default: all.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max sample suspects to print. Default 20.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        help=(
            "Exit non-zero if the suspect rate (suspects/total) exceeds "
            "this fraction. Default 0.0 — any suspect fails."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    since = _parse_since(args.since)
    agent = ChatAgent(args.agent) if args.agent else None

    total, suspect_count, samples = await _audit(
        since=since, agent=agent, limit=args.limit
    )

    rate = (suspect_count / total) if total else 0.0
    logger.info(
        "Audited %d assistant turns since %s (agent=%s)",
        total,
        since.isoformat(),
        agent.value if agent else "all",
    )
    logger.info(
        "Hand-back suspects: %d (%.1f%%)",
        suspect_count,
        rate * 100,
    )

    for s in samples:
        logger.info(
            "  [%s] %s/%s — match=%r — %s",
            s["created_at"],
            s["agent"],
            s["conversation_id"],
            s["matched_phrase"],
            s["content_preview"],
        )

    if rate > args.threshold:
        logger.warning(
            "Hand-back rate %.2f%% exceeds threshold %.2f%% — "
            "investigate and tighten the prompt or the tool contract.",
            rate * 100,
            args.threshold * 100,
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
