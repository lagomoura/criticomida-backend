"""Delete polymorphic image rows (and upload files) when an entity is removed."""

import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.image import EntityType, Image

UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    'uploads',
)


async def _delete_image_files(db: AsyncSession, images: list[Image]) -> None:
    for image in images:
        if image.url.startswith('/uploads/'):
            filepath = os.path.join(UPLOAD_DIR, os.path.basename(image.url))
            if os.path.exists(filepath):
                os.remove(filepath)
        await db.delete(image)


async def delete_images_for_restaurant(
    db: AsyncSession,
    restaurant_id: uuid.UUID,
) -> None:
    """Remove cover, gallery, and menu images tied to a restaurant UUID."""
    types = (
        EntityType.restaurant_cover,
        EntityType.restaurant_gallery,
        EntityType.menu,
    )
    result = await db.execute(
        select(Image).where(
            Image.entity_id == restaurant_id,
            Image.entity_type.in_(types),
        )
    )
    images = list(result.scalars().all())
    await _delete_image_files(db, images)


async def delete_images_for_dish(
    db: AsyncSession,
    dish_id: uuid.UUID,
) -> None:
    """Remove dish cover images for the given dish."""
    result = await db.execute(
        select(Image).where(
            Image.entity_id == dish_id,
            Image.entity_type == EntityType.dish_cover,
        )
    )
    images = list(result.scalars().all())
    await _delete_image_files(db, images)
