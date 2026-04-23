"""Feed endpoint: GET /api/feed + GET /api/reviews/{id}.

Both return the same ReviewPost shape that the social UI consumes. The feed
filters by `type` (`for_you` or `following`) and paginates with an ISO
timestamp cursor against `created_at DESC`. The review-detail endpoint is a
single-row variant that also bundles review extras (pros/cons/tags).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user_optional
from app.models.category import Category
from app.models.dish import (
    Dish,
    DishReview,
    DishReviewImage,
    DishReviewProsCons,
    DishReviewTag,
)
from app.models.follow import Follow
from app.models.like import Like
from app.models.restaurant import Restaurant
from app.models.social import Bookmark, Comment
from app.models.user import User
from app.schemas.feed import (
    FeedAuthor,
    FeedDish,
    FeedExtras,
    FeedItem,
    FeedMediaImage,
    FeedPage,
    FeedStats,
    FeedViewerState,
)

router = APIRouter(tags=["feed"])


ANONYMOUS_AUTHOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _author_for_row(user: User, is_anonymous: bool) -> FeedAuthor:
    if is_anonymous:
        return FeedAuthor(
            id=ANONYMOUS_AUTHOR_ID,
            display_name="Anónimo",
            handle=None,
            avatar_url=None,
        )
    return FeedAuthor(
        id=user.id,
        display_name=user.display_name,
        handle=user.handle,
        avatar_url=user.avatar_url,
    )


async def _images_for_reviews(
    db: AsyncSession, review_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[FeedMediaImage]]:
    """Fetch images grouped by review_id. Empty dict when no reviews."""
    if not review_ids:
        return {}
    result = await db.execute(
        select(DishReviewImage)
        .where(DishReviewImage.dish_review_id.in_(review_ids))
        .order_by(DishReviewImage.dish_review_id, DishReviewImage.display_order)
    )
    out: dict[uuid.UUID, list[FeedMediaImage]] = {}
    for img in result.scalars():
        out.setdefault(img.dish_review_id, []).append(
            FeedMediaImage(url=img.url, alt=img.alt_text)
        )
    return out


def _diversify_by_restaurant(rows: list) -> list:
    """Reorder so consecutive items don't share a restaurant when avoidable.

    Single-pass greedy: keep the order mostly intact but bubble the next item
    forward if its restaurant equals the previous. If no alternative is
    available within a small look-ahead window, leave it (some repeats are
    unavoidable with sparse data).
    """
    if len(rows) <= 2:
        return rows
    result = [rows[0]]
    pending = list(rows[1:])
    prev_restaurant_id = rows[0][3].id  # rows: (review, user, dish, restaurant, ...)
    while pending:
        chosen_idx = 0
        for i, row in enumerate(pending[:5]):  # window of 5 to keep it cheap
            if row[3].id != prev_restaurant_id:
                chosen_idx = i
                break
        chosen = pending.pop(chosen_idx)
        result.append(chosen)
        prev_restaurant_id = chosen[3].id
    return result


async def _build_feed_items(
    db: AsyncSession,
    viewer: User | None,
    base_filters: list,
    cursor_dt: datetime | None,
    limit: int,
    *,
    with_extras: bool = False,
    rank_by_priority: bool = False,
    diversify_by_restaurant: bool = False,
) -> tuple[list[FeedItem], bool]:
    """Common query builder shared by feed list and single-review lookup.

    Returns (items, has_more). `base_filters` is extended with the cursor
    predicate if present. `with_extras` controls whether we hydrate
    pros_cons/tags (skip in feed list; include in review detail).

    When `rank_by_priority` is on (the `for_you` heuristic), the query orders
    by a composite priority (rating + engagement + recency boosts) before
    falling back to `created_at DESC` for ties. `diversify_by_restaurant`
    reshuffles the returned page to avoid consecutive entries from the same
    restaurant.
    """
    viewer_id = viewer.id if viewer else None

    # Count sub-selects (correlated) — evaluated per row but let PostgreSQL
    # cache plan. For MVP scale this is acceptable; optimize if feed gets hot.
    likes_count = (
        select(func.count())
        .select_from(Like)
        .where(Like.review_id == DishReview.id)
        .correlate(DishReview)
        .scalar_subquery()
    )
    comments_count = (
        select(func.count())
        .select_from(Comment)
        .where(Comment.review_id == DishReview.id, Comment.removed_at.is_(None))
        .correlate(DishReview)
        .scalar_subquery()
    )
    saves_count = (
        select(func.count())
        .select_from(Bookmark)
        .where(Bookmark.review_id == DishReview.id)
        .correlate(DishReview)
        .scalar_subquery()
    )

    # Viewer-state EXISTS clauses resolve to `false` when there's no session.
    if viewer_id is not None:
        viewer_liked = exists().where(
            Like.review_id == DishReview.id,
            Like.user_id == viewer_id,
        ).correlate(DishReview)
        viewer_saved = exists().where(
            Bookmark.review_id == DishReview.id,
            Bookmark.user_id == viewer_id,
        ).correlate(DishReview)
        viewer_follows_author = exists().where(
            Follow.follower_id == viewer_id,
            Follow.following_id == DishReview.user_id,
        ).correlate(DishReview)
    else:
        viewer_liked = select(False).scalar_subquery()
        viewer_saved = select(False).scalar_subquery()
        viewer_follows_author = select(False).scalar_subquery()

    stmt = (
        select(
            DishReview,
            User,
            Dish,
            Restaurant,
            Category,
            likes_count.label("likes_count"),
            comments_count.label("comments_count"),
            saves_count.label("saves_count"),
            viewer_liked.label("viewer_liked"),
            viewer_saved.label("viewer_saved"),
            viewer_follows_author.label("viewer_follows_author"),
        )
        .join(User, DishReview.user_id == User.id)
        .join(Dish, DishReview.dish_id == Dish.id)
        .join(Restaurant, Dish.restaurant_id == Restaurant.id)
        .outerjoin(Category, Restaurant.category_id == Category.id)
    )

    for predicate in base_filters:
        stmt = stmt.where(predicate)
    if cursor_dt is not None:
        stmt = stmt.where(DishReview.created_at < cursor_dt)

    if rank_by_priority:
        # Composite ranking: quality (rating ≥ 4 → +2), engagement (scaled so
        # one like ≈ 0.1 units, one comment ≈ 0.2 units), and recency (last
        # week → +2). Ties break by created_at DESC. The cursor still filters
        # by created_at because mixing cursor semantics with ranking would
        # destabilize the page boundaries. This is a sliding window: each
        # page is the best-ranked slice of reviews older than the cursor.
        recent_cutoff = func.now() - text("interval '7 days'")
        priority = (
            case((DishReview.rating >= 4, 2), else_=0)
            + (likes_count * 0.1)
            + (comments_count * 0.2)
            + case((DishReview.created_at >= recent_cutoff, 2), else_=0)
        )
        stmt = stmt.order_by(priority.desc(), DishReview.created_at.desc())
    else:
        stmt = stmt.order_by(DishReview.created_at.desc())

    # Over-fetch a bit when diversifying so reshuffling still yields a full
    # page; otherwise the last item could slip to the next page.
    fetch_limit = limit + (1 if not diversify_by_restaurant else max(5, limit // 2))
    stmt = stmt.limit(fetch_limit + 1)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > fetch_limit
    trimmed = rows[:fetch_limit]

    if diversify_by_restaurant and len(trimmed) > 1:
        trimmed = _diversify_by_restaurant(trimmed)
    # After diversification we still return `limit` items.
    trimmed = trimmed[:limit]

    review_ids = [r[0].id for r in trimmed]
    images_by_review = await _images_for_reviews(db, review_ids)

    pros_cons_by_review: dict[uuid.UUID, tuple[list[str], list[str]]] = {}
    tags_by_review: dict[uuid.UUID, list[str]] = {}
    if with_extras and review_ids:
        pc_rows = (
            await db.execute(
                select(DishReviewProsCons).where(
                    DishReviewProsCons.dish_review_id.in_(review_ids)
                )
            )
        ).scalars().all()
        for row in pc_rows:
            pros, cons = pros_cons_by_review.setdefault(row.dish_review_id, ([], []))
            (pros if row.type.value == "pro" else cons).append(row.text)

        tag_rows = (
            await db.execute(
                select(DishReviewTag).where(
                    DishReviewTag.dish_review_id.in_(review_ids)
                )
            )
        ).scalars().all()
        for row in tag_rows:
            tags_by_review.setdefault(row.dish_review_id, []).append(row.tag)

    items: list[FeedItem] = []
    for (
        review,
        author,
        dish,
        restaurant,
        category,
        likes_c,
        comments_c,
        saves_c,
        v_liked,
        v_saved,
        v_follow_author,
    ) in trimmed:
        extras: FeedExtras | None = None
        if with_extras:
            pros, cons = pros_cons_by_review.get(review.id, ([], []))
            extras = FeedExtras(
                portion_size=review.portion_size.value
                if review.portion_size is not None
                else None,
                would_order_again=review.would_order_again,
                pros=pros,
                cons=cons,
                tags=tags_by_review.get(review.id, []),
                date_tasted=review.date_tasted if isinstance(review.date_tasted, date) else None,
                visited_with=review.visited_with,
                is_anonymous=review.is_anonymous,
                price_tier=dish.price_tier.value if dish.price_tier is not None else None,
            )

        items.append(
            FeedItem(
                id=review.id,
                created_at=review.created_at,
                author=_author_for_row(author, bool(review.is_anonymous)),
                dish=FeedDish(
                    id=dish.id,
                    name=dish.name,
                    restaurant_id=restaurant.id,
                    restaurant_name=restaurant.name,
                    category=category.name if category else None,
                ),
                score=review.rating,
                text=review.note,
                media=images_by_review.get(review.id, []),
                stats=FeedStats(
                    likes=int(likes_c or 0),
                    comments=int(comments_c or 0),
                    saves=int(saves_c or 0),
                ),
                viewer_state=FeedViewerState(
                    liked=bool(v_liked),
                    saved=bool(v_saved),
                    following_author=bool(v_follow_author),
                ),
                extras=extras,
            )
        )

    return items, has_more


@router.get("/api/feed", response_model=FeedPage)
async def get_feed(
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    type: Literal["for_you", "following"] = Query(default="for_you"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
) -> FeedPage:
    cursor_dt: datetime | None = None
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Cursor inválido")

    base_filters: list = []

    if type == "following":
        if viewer is None:
            # No session → nothing to follow; return empty page.
            return FeedPage(items=[], next_cursor=None)
        base_filters.append(
            DishReview.user_id.in_(
                select(Follow.following_id).where(Follow.follower_id == viewer.id)
            )
        )

    items, has_more = await _build_feed_items(
        db,
        viewer,
        base_filters,
        cursor_dt,
        limit,
        with_extras=False,
        rank_by_priority=(type == "for_you"),
        diversify_by_restaurant=(type == "for_you"),
    )
    next_cursor = items[-1].created_at.isoformat() if has_more and items else None
    return FeedPage(items=items, next_cursor=next_cursor)


@router.get("/api/reviews/{review_id}", response_model=FeedItem)
async def get_review_detail(
    review_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> FeedItem:
    items, _ = await _build_feed_items(
        db,
        viewer,
        base_filters=[DishReview.id == review_id],
        cursor_dt=None,
        limit=1,
        with_extras=True,
    )
    if not items:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reseña no encontrada",
        )
    return items[0]
