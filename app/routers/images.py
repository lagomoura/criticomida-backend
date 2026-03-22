import os
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.image import EntityType, Image
from app.models.user import User, UserRole
from app.schemas.image import ImageResponse

router = APIRouter(prefix="/api/images", tags=["images"])

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")


@router.post("/upload", response_model=ImageResponse, status_code=status.HTTP_201_CREATED)
async def upload_image(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    entity_type: Annotated[EntityType, Form()],
    entity_id: Annotated[uuid.UUID, Form()],
    file: UploadFile = File(...),
    alt_text: Annotated[str | None, Form()] = None,
    display_order: Annotated[int, Form()] = 0,
) -> Image:
    # Ensure upload directory exists
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # Generate unique filename
    ext = ""
    if file.filename:
        ext = os.path.splitext(file.filename)[1]
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)

    # Save file
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)

    # Create URL (relative path)
    url = f"/uploads/{filename}"

    image = Image(
        entity_type=entity_type,
        entity_id=entity_id,
        url=url,
        alt_text=alt_text,
        display_order=display_order,
    )
    db.add(image)
    await db.flush()
    await db.refresh(image)
    return image


@router.delete("/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_image(
    image_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    result = await db.execute(select(Image).where(Image.id == image_id))
    image = result.scalar_one_or_none()
    if image is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found",
        )

    # Only admin or the uploader (we don't track uploader in the Image model,
    # so only admin can delete for now; or we allow any authenticated user)
    if current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can delete images",
        )

    # Try to delete file from disk
    if image.url.startswith("/uploads/"):
        filepath = os.path.join(UPLOAD_DIR, os.path.basename(image.url))
        if os.path.exists(filepath):
            os.remove(filepath)

    await db.delete(image)
    await db.flush()
