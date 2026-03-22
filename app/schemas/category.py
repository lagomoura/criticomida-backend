from pydantic import BaseModel, Field


class CategoryCreate(BaseModel):
    slug: str = Field(max_length=100)
    name: str = Field(max_length=100)
    description: str | None = Field(None, max_length=500)
    image_url: str | None = None
    display_order: int = 0


class CategoryUpdate(BaseModel):
    slug: str | None = Field(None, max_length=100)
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=500)
    image_url: str | None = None
    display_order: int | None = None


class CategoryResponse(BaseModel):
    id: int
    slug: str
    name: str
    description: str | None
    image_url: str | None
    display_order: int

    model_config = {"from_attributes": True}
