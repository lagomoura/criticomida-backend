import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EntityType(str, enum.Enum):
    restaurant_cover = "restaurant_cover"
    restaurant_gallery = "restaurant_gallery"
    restaurant_official_photo = "restaurant_official_photo"
    dish_cover = "dish_cover"
    menu = "menu"
    # Foto adjunta a un mensaje del chat (ej. el comensal manda una
    # foto al Sommelier para que identifique el plato). El ``entity_id``
    # puede ser la conversación cuando ya existe; en el primer turno
    # es un UUID generado por el cliente — no hay constraint FK porque
    # el entity_type abarca varios destinos heterogéneos.
    chat_attachment = "chat_attachment"
    # Foto de perfil del usuario. ``entity_id`` es el ``users.id`` del
    # uploader; el router exige ``entity_id == current_user.id`` para
    # impedir que un usuario asocie uploads al perfil de otro.
    user_avatar = "user_avatar"


class Image(Base):
    __tablename__ = "images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, name="entity_type"), nullable=False, index=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    alt_text: Mapped[str | None] = mapped_column(String(300), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # Nullable because legacy rows pre-date this column. Migration 054
    # back-fills ``dish_cover`` rows from ``dishes.created_by`` as a
    # best-effort approximation; everything else stays NULL. The
    # router falls back to admin-only delete when this is NULL — same
    # behaviour as before, no regression for legacy data.
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
