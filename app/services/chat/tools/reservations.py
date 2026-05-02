"""``request_reservation`` lets the Sommelier book a table for the
user — with two paths:

- **Restaurant has a verified owner** (``claimed_by_user_id`` set):
  we drop a ``reservation_requests`` row, raise an in-app
  ``Notification`` for the owner, and fire-and-forget a Resend email.
- **No owner**: we don't write anything and the bot just hands back
  the existing partner deeplink (``Restaurant.reservation_url``) so
  the user can book through the existing affiliate flow.

The tool reports which path it took so the FE can show the right
follow-up card (booking-confirmation vs deeplink CTA).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reservation_request import ReservationRequest, ReservationStatus
from app.models.restaurant import Restaurant
from app.models.social import Notification
from app.models.user import User
from app.services.chat.agent_loop import ToolSpec
from app.services.email_service import render_reservation_requested, send_email


logger = logging.getLogger(__name__)


REQUEST_RESERVATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "restaurant_id": {"type": "string", "format": "uuid"},
        "party_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30,
        },
        "requested_for": {
            "type": "string",
            "description": (
                "ISO 8601 date-time of when the user wants the table. "
                "Always include timezone offset; if unsure, use the "
                "restaurant's local time and -03:00 as a default."
            ),
        },
        "message": {
            "type": "string",
            "maxLength": 600,
            "description": (
                "Optional note from the user (allergies, occasion). The "
                "owner sees it verbatim."
            ),
        },
    },
    "required": ["restaurant_id", "party_size", "requested_for"],
    "additionalProperties": False,
}


def _format_for_human(dt: datetime) -> str:
    # Owners read this in Argentina-leaning Spanish; keep it short.
    return dt.strftime("%d/%m/%Y a las %H:%M")


def make_request_reservation_tool(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if user_id is None:
            return {
                "error": (
                    "User not authenticated. Ask them to log in to "
                    "request a table."
                )
            }

        try:
            requested_for = datetime.fromisoformat(
                args["requested_for"].replace("Z", "+00:00")
            )
        except ValueError:
            return {
                "error": (
                    "requested_for must be a valid ISO 8601 datetime "
                    "with timezone offset."
                )
            }
        if requested_for.tzinfo is None:
            return {
                "error": "requested_for must include a timezone offset.",
            }
        if requested_for < datetime.now(timezone.utc):
            return {
                "error": "requested_for must be in the future.",
            }

        restaurant_id = uuid.UUID(args["restaurant_id"])
        rest = (
            await db.execute(
                select(Restaurant).where(Restaurant.id == restaurant_id)
            )
        ).scalars().first()
        if rest is None:
            return {"error": "Restaurant not found."}

        # No owner: hand off to the affiliate deeplink path.
        if rest.claimed_by_user_id is None:
            return {
                "status": "deeplink",
                "restaurant_id": str(rest.id),
                "restaurant_slug": rest.slug,
                "restaurant_name": rest.name,
                "reservation_url": rest.reservation_url,
                "reservation_provider": rest.reservation_provider,
                "message": (
                    "Este restaurante no tiene equipo verificado en "
                    "CritiComida. Te paso el link directo del partner."
                    if rest.reservation_url
                    else "No hay canal de reserva activo todavía."
                ),
            }

        owner_id = rest.claimed_by_user_id
        request_row = ReservationRequest(
            id=uuid.uuid4(),
            requester_user_id=user_id,
            restaurant_id=rest.id,
            owner_user_id=owner_id,
            party_size=int(args["party_size"]),
            requested_for=requested_for,
            message=args.get("message"),
            status=ReservationStatus.pending,
            source_conversation_id=conversation_id,
        )
        db.add(request_row)

        notif = Notification(
            id=uuid.uuid4(),
            recipient_user_id=owner_id,
            actor_user_id=user_id,
            kind="reservation_requested",
            target_restaurant_id=rest.id,
            text=(
                f"Pidieron mesa para {request_row.party_size} pax "
                f"el {_format_for_human(requested_for)}."
            ),
        )
        db.add(notif)
        await db.flush()

        # Email is fire-and-forget: the row is already committed, so a
        # mailer hiccup must never roll back the user-visible action.
        requester = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalars().first()
        owner = (
            await db.execute(select(User).where(User.id == owner_id))
        ).scalars().first()
        if owner and owner.email:
            subject, html, text = render_reservation_requested(
                restaurant_name=rest.name,
                restaurant_slug=rest.slug,
                requester_name=(
                    requester.display_name if requester else "Un comensal"
                ),
                party_size=request_row.party_size,
                requested_for_human=_format_for_human(requested_for),
                message=request_row.message,
            )

            async def _send() -> None:
                try:
                    await send_email(
                        to=owner.email, subject=subject, html=html, text=text
                    )
                except Exception:
                    logger.exception("reservation_requested email failed")

            asyncio.create_task(_send())

        return {
            "status": "requested",
            "request_id": str(request_row.id),
            "restaurant_id": str(rest.id),
            "restaurant_slug": rest.slug,
            "restaurant_name": rest.name,
            "party_size": request_row.party_size,
            "requested_for": requested_for.isoformat(),
            "owner_will_be_notified": True,
        }

    return ToolSpec(
        name="request_reservation",
        description=(
            "Request a table at a restaurant. When the restaurant has a "
            "verified owner on CritiComida, the request is delivered to "
            "their dashboard and email; otherwise the tool returns the "
            "partner deeplink so the user can book externally. Requires "
            "an authenticated user."
        ),
        input_schema=REQUEST_RESERVATION_SCHEMA,
        handler=handler,
        emits_card=True,
    )
