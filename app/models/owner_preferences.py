"""Preferencias del verified owner por restaurante.

Dos tablas separadas que comparten la misma clave compuesta
``(user_id, restaurant_id)`` pero responden a productos distintos:

- ``owner_notification_preferences``: toggle de notificaciones cuando
  llega una reseña nueva (push del backend a email/in-app).
- ``owner_chat_preferences``: tono, KPIs e idioma que el chat Business
  inyecta al system prompt. Se actualizan vía el tool
  ``update_owner_preferences`` durante una conversación; el agente
  los lee al inicio de cada sesión para personalizar el saludo y la
  redacción.

Mantenemos las dos tablas separadas porque sus ciclos de actualización
son distintos: la primera la toca un toggle UI, la segunda la toca el
LLM en respuesta a frases del owner.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, PrimaryKeyConstraint, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OwnerNotificationPreference(Base):
    __tablename__ = "owner_notification_preferences"
    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id", "restaurant_id", name="pk_owner_notification_preferences"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
    )
    notify_on_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
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


class OwnerChatPreference(Base):
    """Preferencias del chat Business por (owner, restaurante).

    Sin fila → defaults: el agente trata al owner con tono profesional
    neutro, sin KPIs prioritarios, idioma adaptado al input. La fila
    se crea on-demand cuando el owner pide explícitamente algo
    persistente (idioma, tono, KPIs).
    """

    __tablename__ = "owner_chat_preferences"
    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id", "restaurant_id", name="pk_owner_chat_preferences"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Enums kept as plain strings (CHECK at app level via Pydantic) to
    # avoid coupling the DB to the chat tone catalogue. New values can
    # ship without a migration.
    tone_preference: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    kpi_focus: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True
    )
    language_preference: Mapped[str | None] = mapped_column(
        String(8), nullable=True
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
