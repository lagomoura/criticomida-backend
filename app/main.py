from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import engine
from app.routers import (
    admin,
    auth,
    categories,
    dishes,
    images,
    menus,
    ratings,
    restaurants,
    reviews,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup
    import os
    os.makedirs(os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads"), exist_ok=True)

    # Auto-create tables in development
    from app.database import Base
    import app.models  # noqa: F401 - ensure all models are imported
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield
    # Shutdown
    await engine.dispose()


app = FastAPI(
    title="CritiComida API",
    description="Food review platform where users review individual dishes",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router)
app.include_router(categories.router)
app.include_router(restaurants.router)
app.include_router(dishes.router)
app.include_router(reviews.router)
app.include_router(ratings.router)
app.include_router(images.router)
app.include_router(menus.router)
app.include_router(admin.router)

# Serve uploaded files
import os
uploads_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
os.makedirs(uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
