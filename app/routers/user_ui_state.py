"""User UI state endpoints (B2C).

Tres surfaces, todos gateados por usuario autenticado:

- ``GET    /api/users/me/ui-state`` — lectura del estado completo.
- ``POST   /api/users/me/ui-state/dismiss-tour`` — descartar un tour
  específico. Upsert atómico (race-safe entre pestañas).
- ``DELETE /api/users/me/ui-state/dismissed-tours/{tour_id}`` —
  re-habilitar un tour ("Volver a ver el recorrido" en
  ``/me/preferencias``).

POST + path-style por id en lugar de PUT con array completo: el
cliente nunca sobreescribe el set entero, así dos pestañas
descartando tours distintos no se pisan.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User


router = APIRouter(tags=["user-ui-state"])


# ──────────────────────────────────────────────────────────────────────────
#   Schemas
# ──────────────────────────────────────────────────────────────────────────

_TOUR_ID_PATTERN = r"^[a-z0-9_]+$"


class UIStateRead(BaseModel):
    """Vista serializada del estado de UI."""

    dismissed_tours: list[str] = Field(default_factory=list)


class DismissTourPayload(BaseModel):
    tour_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=_TOUR_ID_PATTERN,
        description="Identificador del tour a descartar, e.g. 'home_v1'.",
    )


# ──────────────────────────────────────────────────────────────────────────
#   Endpoints
# ──────────────────────────────────────────────────────────────────────────


@router.get(
    "/api/users/me/ui-state",
    response_model=UIStateRead,
)
async def get_my_ui_state(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> UIStateRead:
    """Sin fila → array vacío. No creamos la fila en el read para
    mantener el GET barato y evitar writes implícitos."""
    row = (
        await db.execute(
            text("SELECT dismissed_tours FROM user_ui_state WHERE user_id = :uid"),
            {"uid": current_user.id},
        )
    ).first()
    if row is None:
        return UIStateRead(dismissed_tours=[])
    return UIStateRead(dismissed_tours=list(row[0] or []))


@router.post(
    "/api/users/me/ui-state/dismiss-tour",
    response_model=UIStateRead,
)
async def dismiss_tour(
    payload: DismissTourPayload,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> UIStateRead:
    """Upsert atómico: si no hay fila la crea con ``[tour_id]``; si
    hay, hace ``array_agg(DISTINCT existente || nuevo)`` y persiste.
    Idempotente — descartar un tour dos veces deja el set igual."""
    row = (
        await db.execute(
            text(
                """
                INSERT INTO user_ui_state (user_id, dismissed_tours, created_at, updated_at)
                VALUES (:uid, ARRAY[:tid]::text[], now(), now())
                ON CONFLICT (user_id) DO UPDATE
                  SET dismissed_tours = (
                        SELECT array_agg(DISTINCT x)
                          FROM unnest(user_ui_state.dismissed_tours || EXCLUDED.dismissed_tours) AS x
                      ),
                      updated_at = now()
                RETURNING dismissed_tours
                """
            ),
            {"uid": current_user.id, "tid": payload.tour_id},
        )
    ).first()
    await db.commit()
    assert row is not None  # RETURNING garantiza fila
    return UIStateRead(dismissed_tours=list(row[0] or []))


@router.delete(
    "/api/users/me/ui-state/dismissed-tours/{tour_id}",
    response_model=UIStateRead,
)
async def restore_tour(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    tour_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=64,
            pattern=_TOUR_ID_PATTERN,
        ),
    ],
) -> UIStateRead:
    """Re-habilita un tour. Idempotente: si el tour no estaba
    descartado, la respuesta es el mismo array que ya estaba."""
    row = (
        await db.execute(
            text(
                """
                UPDATE user_ui_state
                   SET dismissed_tours = array_remove(dismissed_tours, :tid),
                       updated_at = now()
                 WHERE user_id = :uid
                 RETURNING dismissed_tours
                """
            ),
            {"uid": current_user.id, "tid": tour_id},
        )
    ).first()
    await db.commit()
    if row is None:
        # No había fila → no había nada que restaurar. Devolver vacío.
        return UIStateRead(dismissed_tours=[])
    return UIStateRead(dismissed_tours=list(row[0] or []))
