"""Per-user UI state (out-of-band del catálogo y del chat).

Aislada de ``user_chat_preferences`` a propósito: ese agregado se
serializa en el system prompt del Sommelier en cada sesión de chat
(`render_user_preferences_block`). Si metiéramos acá los ``dismissed_tours``
contaminaríamos un payload conversacional con datos puramente de UI.

Una sola fila por ``user_id``. ``dismissed_tours`` es un array de
identificadores de tours que el usuario ya descartó (e.g.
``home_v1``, ``owner_dashboard_v1``). Append-only desde el cliente;
los borrados existen solo para el caso de "Volver a ver el recorrido".
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Text

from app.database import Base


class UserUIState(Base):
    """Estado de UI persistente por usuario (cross-device).

    Hoy guarda solo ``dismissed_tours``. Espacio para crecer sin tocar
    la tabla del chat (e.g. ``last_seen_changelog_version``, flags de
    onboarding de owner, etc.).
    """

    __tablename__ = "user_ui_state"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", name="pk_user_ui_state"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Append-only set de tour_ids ya cerrados por el usuario. Los
    # ids son strings cortos ([a-z0-9_]{1,64}) validados en la API
    # — el catálogo de tours es del FE y no necesita FK.
    dismissed_tours: Mapped[list[str]] = mapped_column(
        ARRAY(Text()),
        nullable=False,
        server_default="{}",
        default=list,
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
