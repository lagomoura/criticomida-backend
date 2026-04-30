import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole

MasteryLevel = Literal["apprentice", "sommelier", "master"]


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)


class UserUpdate(BaseModel):
    """Legacy update schema used by admin endpoints."""

    display_name: str | None = Field(None, min_length=1, max_length=100)
    avatar_url: str | None = None


class UserProfileUpdate(BaseModel):
    """Payload for the logged-in user to edit their own public profile."""

    display_name: str | None = Field(None, min_length=1, max_length=100)
    handle: str | None = Field(
        None,
        min_length=3,
        max_length=30,
        pattern=r"^[a-zA-Z0-9_]+$",
    )
    bio: str | None = Field(None, max_length=500)
    location: str | None = Field(None, max_length=200)
    avatar_url: str | None = None


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    avatar_url: str | None
    handle: str | None = None
    bio: str | None = None
    location: str | None = None
    role: UserRole
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserCounts(BaseModel):
    reviews: int = 0
    followers: int = 0
    following: int = 0


class CategoryStat(BaseModel):
    """Una categoría donde el usuario muestra criterio (volumen + rating).

    `score` es el ranking interno (rating × log(1+count)) que usa el backend
    para elegir las top categorías; el frontend lo ignora a la hora de
    renderizar pero puede usarlo para tie-breaking.

    `mastery_level` clasifica al usuario en esa categoría según volumen y
    promedio. Niveles escalonados: aprendiz → sommelier → maestro. None
    cuando no llega al umbral mínimo (3 reseñas y avg ≥ 3.5).
    """

    name: str
    review_count: int
    avg_rating: float
    score: float
    mastery_level: MasteryLevel | None = None


class FeaturedTitle(BaseModel):
    """Título destacado que el usuario "lleva" en el feed (chip junto al nombre).

    Se elige el de mayor nivel de maestría; tie-break por mayor `review_count`.
    None cuando ninguna categoría alcanza el nivel `apprentice`.
    """

    category: str
    level: MasteryLevel


class UserReputation(BaseModel):
    """Métricas que cuantifican la "voz del crítico" de un usuario.

    - `verified_review_count`: reviews con los 3 pilares técnicos completos.
      Indica cuán seriamente el usuario aplica criterio en sus reseñas.
    - `restaurants_visited`: cantidad de restaurantes únicos reseñados.
    - `top_categories`: hasta 3 categorías donde el usuario tiene volumen
      ≥ 2 y mejor combinación de volumen + rating. Sirve como "especialidad"
      del crítico.
    - `featured_title`: el título de maestría más alto alcanzado, listo para
      renderizar como chip junto al nombre.
    """

    verified_review_count: int = 0
    restaurants_visited: int = 0
    top_categories: list[CategoryStat] = []
    featured_title: FeaturedTitle | None = None


class PublicViewerState(BaseModel):
    """Describes the relationship between the caller and the profile owner.

    Populated only when the request carries a valid session; anonymous callers
    receive the default `False`s.
    """

    is_self: bool = False
    following: bool = False


class PublicUserResponse(BaseModel):
    """Public-facing profile consumed by /u/[id_or_handle]."""

    id: uuid.UUID
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    location: str | None = None
    counts: UserCounts
    reputation: UserReputation = UserReputation()
    viewer_state: PublicViewerState

    model_config = {"from_attributes": True}


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    refresh_token: str | None = None
