"""B2C-side preferences — the Sommelier's mirror of
``owner_chat_preferences``.

One row per ``user_id`` (no restaurant scope — the Sommelier sees the
whole catalog). The row is created on-demand the first time the
comensal asks for something persistent ("siempre respondé en inglés",
"hablame corto") via the chat tool ``update_user_chat_preferences``,
or via the form on ``/me/preferencias``.

Sin fila → defaults from the prompt: the agent adapts language to
the input and uses the "editorial" voice CritiComida default.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, PrimaryKeyConstraint, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserChatPreference(Base):
    """Persistent chat preferences for a B2C comensal.

    Two narrow knobs the comensal can pin from one session to the
    next:

    - ``language_preference`` (``es``/``en``/``pt``): forces the
      Sommelier to answer in this locale regardless of the input
      language. NULL → adapt to whatever the comensal types.
    - ``response_style`` (``editorial``/``concise``/``warm``): tunes
      the framing length. ``editorial`` is the default voice from
      the prompt (2-3 framing sentences); ``concise`` collapses to
      one sentence + cards; ``warm`` adds more conversational
      colour.

    Allergies and ``preferred_hours`` are NOT here — those live on
    ``UserTasteProfile`` because they're catalog-side preferences
    (used by tools that read profile, not just the chat surface).
    """

    __tablename__ = "user_chat_preferences"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", name="pk_user_chat_preferences"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Strings instead of DB-level enums so adding new values doesn't
    # require a migration. Pydantic on the API layer enforces the
    # closed enum.
    language_preference: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )
    response_style: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
