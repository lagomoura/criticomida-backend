import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.dish import Dish, DishReview
from app.models.restaurant import Restaurant
from app.models.social import Comment, Report
from app.models.user import User, UserRole
from app.schemas.bookmark_report import (
    ReportAdminResponse,
    ReporterSummary,
    ReportCreate,
    ReportResponse,
    ReportStatusUpdate,
    ReportTargetPreview,
    ReportsPage,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.post("", response_model=ReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    payload: ReportCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Report:
    """
    Users report content for moderation. No validation that `entity_id`
    references a real row — moderators deal with stale pointers in the queue.
    """
    report = Report(
        reporter_user_id=current_user.id,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        reason=payload.reason.strip(),
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report


# ── Admin moderation queue ───────────────────────────────────────────────────

_EXCERPT_LEN = 140


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if len(text) <= _EXCERPT_LEN:
        return text
    return text[:_EXCERPT_LEN].rstrip() + "…"


async def _hydrate_target(
    db: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> ReportTargetPreview:
    """Resolve a polymorphic target to a single-line preview for the admin UI."""
    if entity_type == "review":
        row = (
            await db.execute(
                select(DishReview.note, Dish.name, Restaurant.name)
                .join(Dish, DishReview.dish_id == Dish.id)
                .join(Restaurant, Dish.restaurant_id == Restaurant.id)
                .where(DishReview.id == entity_id)
            )
        ).first()
        if row is None:
            return ReportTargetPreview(kind="review", id=entity_id, deleted=True)
        note, dish_name, restaurant_name = row
        preview = f"{dish_name} @ {restaurant_name}: " + (_excerpt(note) or "")
        return ReportTargetPreview(kind="review", id=entity_id, preview=preview)

    if entity_type == "comment":
        row = (
            await db.execute(
                select(Comment.body, Comment.removed_at, Comment.review_id).where(
                    Comment.id == entity_id
                )
            )
        ).first()
        if row is None:
            return ReportTargetPreview(kind="comment", id=entity_id, deleted=True)
        body, removed_at, review_id = row
        if removed_at is not None:
            return ReportTargetPreview(
                kind="comment",
                id=entity_id,
                preview="[comentario removido]",
                deleted=True,
                parent_id=review_id,
            )
        return ReportTargetPreview(
            kind="comment",
            id=entity_id,
            preview=_excerpt(body),
            parent_id=review_id,
        )

    if entity_type == "user":
        row = (
            await db.execute(
                select(User.display_name, User.handle).where(User.id == entity_id)
            )
        ).first()
        if row is None:
            return ReportTargetPreview(kind="user", id=entity_id, deleted=True)
        display_name, handle = row
        label = display_name + (f" (@{handle})" if handle else "")
        return ReportTargetPreview(kind="user", id=entity_id, preview=label)

    # Unknown entity types shouldn't happen given the CHECK constraint, but
    # we stay defensive so admins never see a 500 in the queue.
    return ReportTargetPreview(kind=entity_type, id=entity_id, deleted=True)  # type: ignore[arg-type]


@router.get("", response_model=ReportsPage)
async def list_reports(
    _admin: Annotated[User, Depends(require_role(UserRole.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
    report_status: Literal["pending", "reviewed", "dismissed"] | None = Query(
        default="pending", alias="status"
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
) -> ReportsPage:
    cursor_dt: datetime | None = None
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Cursor inválido")

    # LEFT JOIN reporter because the FK is ON DELETE SET NULL — account
    # deletions shouldn't drop reports from the queue.
    stmt = (
        select(Report, User)
        .outerjoin(User, Report.reporter_user_id == User.id)
        .order_by(Report.created_at.desc())
        .limit(limit + 1)
    )
    if report_status is not None:
        stmt = stmt.where(Report.status == report_status)
    if cursor_dt is not None:
        stmt = stmt.where(Report.created_at < cursor_dt)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    trimmed = rows[:limit]

    items: list[ReportAdminResponse] = []
    for report, reporter in trimmed:
        target = await _hydrate_target(db, report.entity_type, report.entity_id)
        items.append(
            ReportAdminResponse(
                id=report.id,
                entity_type=report.entity_type,
                entity_id=report.entity_id,
                reason=report.reason,
                status=report.status,
                created_at=report.created_at,
                reporter=ReporterSummary(
                    id=reporter.id if reporter else None,
                    display_name=reporter.display_name if reporter else None,
                    handle=reporter.handle if reporter else None,
                ),
                target=target,
            )
        )

    next_cursor = (
        trimmed[-1][0].created_at.isoformat() if has_more and trimmed else None
    )
    return ReportsPage(items=items, next_cursor=next_cursor)


@router.patch("/{report_id}", response_model=ReportResponse)
async def update_report_status(
    report_id: uuid.UUID,
    payload: ReportStatusUpdate,
    _admin: Annotated[User, Depends(require_role(UserRole.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Report:
    result = await db.execute(select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Reporte no encontrado")
    report.status = payload.status
    await db.commit()
    await db.refresh(report)
    return report
