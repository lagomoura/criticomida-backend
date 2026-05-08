import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.dish import (
    Dish,
    DishReview,
    DishReviewImage,
    DishReviewProsCons,
    DishReviewTag,
)
from app.models.owner_preferences import OwnerNotificationPreference
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.dish import DishReviewCreate, DishReviewResponse, DishReviewUpdate, MyReviewResponse
from app.services.email_service import (
    render_review_on_owned_restaurant,
    send_email,
)
from app.services.notification_service import (
    record_mention_notifications,
    record_review_on_owned_restaurant_notification,
)
from app.services.price_validation import (
    evaluate_price_outlier,
    validate_price_paid,
)
from app.services.embeddings_service import schedule_reembed_review
from app.services.rating_service import update_dish_rating, update_restaurant_rating
from app.services.sentiment_service import schedule_analyze_review

router = APIRouter(tags=["reviews"])


@router.get(
    "/api/users/me/reviews",
    response_model=list[MyReviewResponse],
)
async def get_my_reviews(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[DishReview]:
    result = await db.execute(
        select(DishReview)
        .options(
            selectinload(DishReview.user),
            selectinload(DishReview.pros_cons),
            selectinload(DishReview.tags),
            selectinload(DishReview.images),
            selectinload(DishReview.dish).selectinload(Dish.restaurant),
        )
        .where(DishReview.user_id == current_user.id)
        .order_by(DishReview.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def _load_review_with_relations(
    db: AsyncSession, review_id: uuid.UUID
) -> DishReview | None:
    result = await db.execute(
        select(DishReview)
        .options(
            selectinload(DishReview.user),
            selectinload(DishReview.pros_cons),
            selectinload(DishReview.tags),
            selectinload(DishReview.images),
        )
        .where(DishReview.id == review_id)
    )
    return result.scalar_one_or_none()


@router.get(
    "/api/dishes/{dish_id}/reviews",
    response_model=list[DishReviewResponse],
)
async def list_reviews(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[DishReview]:
    # Verify dish exists
    dish_result = await db.execute(select(Dish).where(Dish.id == dish_id))
    if dish_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dish not found",
        )

    result = await db.execute(
        select(DishReview)
        .options(
            selectinload(DishReview.user),
            selectinload(DishReview.pros_cons),
            selectinload(DishReview.tags),
            selectinload(DishReview.images),
        )
        .where(DishReview.dish_id == dish_id)
        .order_by(DishReview.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


@router.post(
    "/api/dishes/{dish_id}/reviews",
    response_model=DishReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_review(
    dish_id: uuid.UUID,
    review_data: DishReviewCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DishReview:
    # Verify dish exists and get restaurant_id
    dish_result = await db.execute(select(Dish).where(Dish.id == dish_id))
    dish = dish_result.scalar_one_or_none()
    if dish is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dish not found",
        )

    # Sanity-cap del precio según la moneda del restaurante. Si la BD aún no
    # tiene la moneda, cae al rango fallback amplio.
    restaurant_for_currency = await db.get(Restaurant, dish.restaurant_id)
    validate_price_paid(
        review_data.price_paid,
        restaurant_for_currency.currency_code if restaurant_for_currency else None,
    )
    # Capa 2: outlier vs histórico del plato. Soft-flag, no rechaza.
    flagged_at, flag_reason = await evaluate_price_outlier(
        db, dish_id=dish_id, price_paid=review_data.price_paid,
    )

    review = DishReview(
        dish_id=dish_id,
        user_id=current_user.id,
        date_tasted=review_data.date_tasted,
        time_tasted=review_data.time_tasted,
        meal_period=review_data.meal_period,
        note=review_data.note,
        rating=review_data.rating,
        price_paid=review_data.price_paid,
        price_flagged_at=flagged_at,
        price_flag_reason=flag_reason,
        portion_size=review_data.portion_size,
        would_order_again=review_data.would_order_again,
        visited_with=review_data.visited_with,
        is_anonymous=review_data.is_anonymous,
        presentation=review_data.presentation,
        value_prop=review_data.value_prop,
        execution=review_data.execution,
    )
    db.add(review)
    await db.flush()

    # Add pros/cons
    for pc in review_data.pros_cons:
        db.add(DishReviewProsCons(
            dish_review_id=review.id,
            type=pc.type,
            text=pc.text,
        ))

    # Add tags
    for tag_data in review_data.tags:
        db.add(DishReviewTag(
            dish_review_id=review.id,
            tag=tag_data.tag,
        ))

    # Add images
    for img_data in review_data.images:
        db.add(DishReviewImage(
            dish_review_id=review.id,
            url=img_data.url,
            alt_text=img_data.alt_text,
            display_order=img_data.display_order,
        ))

    await db.flush()

    # Recompute ratings
    await update_dish_rating(db, dish_id)
    await update_restaurant_rating(db, dish.restaurant_id)

    # Notificar al verified owner del restaurant si la preferencia lo permite.
    # Default ON: si no hay row en owner_notification_preferences se asume
    # que quiere enterarse. Email + in-app van con el mismo toggle.
    restaurant = await db.get(Restaurant, dish.restaurant_id)
    if (
        restaurant is not None
        and restaurant.claimed_by_user_id is not None
        and restaurant.claimed_by_user_id != current_user.id
    ):
        pref = await db.get(
            OwnerNotificationPreference,
            (restaurant.claimed_by_user_id, restaurant.id),
        )
        notify = pref.notify_on_review if pref is not None else True
        if notify:
            await record_review_on_owned_restaurant_notification(
                db,
                actor_id=current_user.id,
                review_id=review.id,
                restaurant_id=restaurant.id,
                owner_user_id=restaurant.claimed_by_user_id,
                dish_name=dish.name,
                rating=float(review_data.rating),
            )
            owner = await db.get(User, restaurant.claimed_by_user_id)
            if owner is not None and owner.email:
                subject, html, text = render_review_on_owned_restaurant(
                    restaurant_name=restaurant.name,
                    restaurant_slug=restaurant.slug,
                    dish_name=dish.name,
                    rating=float(review_data.rating),
                    reviewer_display_name=current_user.display_name,
                    review_id=str(review.id),
                    is_anonymous=bool(review_data.is_anonymous),
                )
                await send_email(
                    to=owner.email, subject=subject, html=html, text=text
                )

    skip_for_mention: set[uuid.UUID] = set()
    if (
        restaurant is not None
        and restaurant.claimed_by_user_id is not None
        and restaurant.claimed_by_user_id != current_user.id
    ):
        skip_for_mention.add(restaurant.claimed_by_user_id)
    await record_mention_notifications(
        db,
        actor_id=current_user.id,
        body=review.note or "",
        target_kind="post",
        target_review_id=review.id,
        skip_recipient_ids=skip_for_mention,
    )

    # Reload with relationships
    loaded = await _load_review_with_relations(db, review.id)
    schedule_analyze_review(review.id)
    schedule_reembed_review(review.id)
    return loaded  # type: ignore[return-value]


@router.put(
    "/api/dish-reviews/{review_id}",
    response_model=DishReviewResponse,
)
async def update_review(
    review_id: uuid.UUID,
    review_data: DishReviewUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DishReview:
    result = await db.execute(
        select(DishReview).where(DishReview.id == review_id)
    )
    review = result.scalar_one_or_none()
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found",
        )

    # Only the author can update their review
    if review.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update your own reviews",
        )

    # Cuando viene `price_paid` en el payload, aplicamos capa 1 (cap por
    # moneda — puede tirar 422) y capa 2 (re-evaluación del outlier flag).
    # Si el cliente NO mandó `price_paid`, no tocamos nada.
    price_payload_present = "price_paid" in review_data.model_fields_set
    # Capturamos el valor previo ANTES del setattr para alimentar el detector
    # con el self-delta (catchea edits drásticos del propio crítico aunque no
    # haya histórico suficiente del plato).
    previous_price = review.price_paid
    if price_payload_present:
        if review_data.price_paid is not None:
            currency_row = (
                await db.execute(
                    select(Restaurant.currency_code)
                    .join(Dish, Dish.restaurant_id == Restaurant.id)
                    .where(Dish.id == review.dish_id)
                    .limit(1)
                )
            ).first()
            validate_price_paid(
                review_data.price_paid,
                currency_row[0] if currency_row else None,
            )

    # Rename del plato. Re-linkeamos por nombre normalizado: si el normalized
    # no cambia es no-op (no tocamos el Dish compartido por otras reviews).
    # Si cambia, find-or-create dentro del mismo restaurante.
    previous_dish_id = review.dish_id
    if "dish_name" in review_data.model_fields_set and review_data.dish_name is not None:
        cleaned_dish_name = review_data.dish_name.strip()
        if not cleaned_dish_name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dish_name no puede estar vacío.",
            )
        current_dish = await db.get(Dish, review.dish_id)
        if current_dish is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Dish not found",
            )
        new_normalized = (
            await db.execute(select(func.dish_name_normalized(cleaned_dish_name)))
        ).scalar_one()
        if new_normalized != current_dish.name_normalized:
            existing_dish = (
                await db.execute(
                    select(Dish).where(
                        Dish.restaurant_id == current_dish.restaurant_id,
                        Dish.name_normalized == new_normalized,
                    )
                )
            ).scalar_one_or_none()
            if existing_dish is not None:
                review.dish_id = existing_dish.id
            else:
                new_dish = Dish(
                    restaurant_id=current_dish.restaurant_id,
                    name=cleaned_dish_name,
                    price_tier=current_dish.price_tier,
                    created_by=current_user.id,
                )
                db.add(new_dish)
                await db.flush()
                review.dish_id = new_dish.id

    previous_note = review.note
    update_data = review_data.model_dump(
        exclude_unset=True,
        exclude={"pros_cons", "tags", "images", "dish_name"},
    )
    for field, value in update_data.items():
        setattr(review, field, value)

    # Re-evaluamos el flag DESPUÉS del setattr, así `review.price_paid` ya
    # está actualizado. Si el precio se vació a NULL, el flag también se
    # limpia (no tiene sentido conservarlo). Si el precio quedó igual al de
    # antes, igual recomputamos — es barato y mantiene el flag consistente
    # con el histórico actual del plato.
    if price_payload_present:
        if review.price_paid is None:
            review.price_flagged_at = None
            review.price_flag_reason = None
            review.price_flag_resolved_at = None
            review.price_flag_resolved_by = None
        else:
            flagged_at, flag_reason = await evaluate_price_outlier(
                db,
                dish_id=review.dish_id,
                price_paid=review.price_paid,
                exclude_review_id=review.id,
                previous_price=previous_price,
            )
            review.price_flagged_at = flagged_at
            review.price_flag_reason = flag_reason
            # Si el flag se borra (volvió a un valor razonable), también
            # limpiamos los campos de resolución previa para que no quede
            # información huérfana.
            if flagged_at is None:
                review.price_flag_resolved_at = None
                review.price_flag_resolved_by = None
    note_changed = (
        review_data.note is not None and review_data.note != previous_note
    )

    if review_data.pros_cons is not None:
        await db.execute(
            delete(DishReviewProsCons).where(
                DishReviewProsCons.dish_review_id == review.id
            )
        )
        for pc in review_data.pros_cons:
            db.add(
                DishReviewProsCons(
                    dish_review_id=review.id,
                    type=pc.type,
                    text=pc.text,
                )
            )

    if review_data.tags is not None:
        await db.execute(
            delete(DishReviewTag).where(DishReviewTag.dish_review_id == review.id)
        )
        for tag_data in review_data.tags:
            db.add(
                DishReviewTag(
                    dish_review_id=review.id,
                    tag=tag_data.tag,
                )
            )

    if review_data.images is not None:
        await db.execute(
            delete(DishReviewImage).where(
                DishReviewImage.dish_review_id == review.id
            )
        )
        for img_data in review_data.images:
            db.add(
                DishReviewImage(
                    dish_review_id=review.id,
                    url=img_data.url,
                    alt_text=img_data.alt_text,
                    display_order=img_data.display_order,
                )
            )

    await db.flush()

    # Recompute ratings — incluye el dish viejo si el rename re-linkeó la
    # review a otro Dish, así su computed_rating refleja la pérdida.
    if previous_dish_id != review.dish_id:
        await update_dish_rating(db, previous_dish_id)
    await update_dish_rating(db, review.dish_id)

    # Get restaurant_id through dish
    dish_result = await db.execute(select(Dish).where(Dish.id == review.dish_id))
    dish = dish_result.scalar_one()
    await update_restaurant_rating(db, dish.restaurant_id)

    loaded = await _load_review_with_relations(db, review.id)
    if note_changed:
        schedule_analyze_review(review.id)
    # Re-embed siempre que se actualiza: además del note, el texto de
    # _review_text incluye pros/cons y tags, que también pueden haber
    # cambiado en este PUT.
    schedule_reembed_review(review.id)
    return loaded  # type: ignore[return-value]


@router.delete(
    "/api/dish-reviews/{review_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_review(
    review_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    result = await db.execute(
        select(DishReview).where(DishReview.id == review_id)
    )
    review = result.scalar_one_or_none()
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found",
        )

    # Author or admin can delete
    if review.user_id != current_user.id and current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own reviews",
        )

    dish_id = review.dish_id

    # Get restaurant_id before deleting
    dish_result = await db.execute(select(Dish).where(Dish.id == dish_id))
    dish = dish_result.scalar_one()
    restaurant_id = dish.restaurant_id

    await db.delete(review)
    await db.flush()

    # Recompute ratings
    await update_dish_rating(db, dish_id)
    await update_restaurant_rating(db, restaurant_id)
