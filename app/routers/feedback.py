from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user_optional, require_role
from app.models.user import User, UserRole
from app.models.user_feedback import UserFeedback
from app.schemas.feedback import UserFeedbackCreate, UserFeedbackResponse

router = APIRouter(prefix='/api/feedback', tags=['feedback'])


@router.post(
    '',
    response_model=UserFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user_feedback(
    body: UserFeedbackCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
) -> UserFeedback:
    row = UserFeedback(
        user_id=current_user.id if current_user else None,
        category=body.category,
        message=body.message,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


@router.get(
    '',
    response_model=list[UserFeedbackResponse],
)
async def list_user_feedback(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.admin))],
    limit: int = Query(default=50, ge=1, le=200),
) -> list[UserFeedback]:
    result = await db.execute(
        select(UserFeedback)
        .order_by(UserFeedback.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
