import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BookmarkActionResponse(BaseModel):
    review_id: uuid.UUID
    saved: bool
    saves_count: int


class BookmarkedReviewSummary(BaseModel):
    """Minimum set to render a saved item in a list; expand later if needed."""

    review_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class BookmarksPage(BaseModel):
    items: list[BookmarkedReviewSummary]
    next_cursor: str | None = None


class ReportCreate(BaseModel):
    entity_type: Literal["review", "comment", "user"]
    entity_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=500)


class ReportResponse(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    reason: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ReporterSummary(BaseModel):
    id: uuid.UUID | None = None
    display_name: str | None = None
    handle: str | None = None


class ReportTargetPreview(BaseModel):
    """One-line context about what's being reported."""

    kind: Literal["review", "comment", "user"]
    id: uuid.UUID
    preview: str | None = None
    deleted: bool = False
    # For comment targets: the parent review so the admin UI can link to
    # `/reviews/{parent_id}#comments`. None for non-comment targets.
    parent_id: uuid.UUID | None = None


class ReportAdminResponse(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    reason: str
    status: str
    created_at: datetime
    reporter: ReporterSummary
    target: ReportTargetPreview


class ReportsPage(BaseModel):
    items: list[ReportAdminResponse]
    next_cursor: str | None = None


class ReportStatusUpdate(BaseModel):
    status: Literal["pending", "reviewed", "dismissed"]
