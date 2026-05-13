from pydantic import BaseModel, Field


class CategoryCreate(BaseModel):
    slug: str = Field(max_length=100)
    name: str = Field(max_length=100)
    description: str | None = Field(None, max_length=500)
    image_url: str | None = None
    display_order: int = 0
    parent_id: int | None = None


class CategoryUpdate(BaseModel):
    slug: str | None = Field(None, max_length=100)
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=500)
    image_url: str | None = None
    display_order: int | None = None
    parent_id: int | None = None


class CategoryResponse(BaseModel):
    id: int
    slug: str
    name: str
    description: str | None
    image_url: str | None
    display_order: int
    parent_id: int | None = None
    review_count: int = 0

    model_config = {"from_attributes": True}


class CategoryPendingResponse(BaseModel):
    """Vista admin-only de una categoría con `pending_review=True`.

    Incluye el conteo de restaurantes que ya quedaron apuntando a ella
    (para que el admin decida si vale la pena aprobarla o re-asignarlos
    a una existente al rechazar)."""

    id: int
    slug: str
    name: str
    description: str | None
    image_url: str | None
    display_order: int
    parent_id: int | None = None
    restaurant_count: int = 0

    model_config = {"from_attributes": True}


class CategoryRejectRequest(BaseModel):
    """Body del POST /api/categories/{slug}/reject.

    ``target_slug`` es el slug al que mover los restaurantes huérfanos
    antes de borrar la pendiente. Default: ``otros`` (siempre existe)."""

    target_slug: str = Field(default="otros", max_length=100)
