import asyncio
import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from starlette.types import Lifespan

from app.config import settings
from app.database import engine
from app.middleware.rate_limit import limiter
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.services.async_job_worker import run_worker_loop


logger = logging.getLogger(__name__)

# Sentry init debe correr ANTES de instanciar FastAPI para que
# StarletteIntegration + FastApiIntegration se auto-detecten y enganchen
# el lifecycle de cada request. El guard permite correr local sin DSN.
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.SENTRY_ENVIRONMENT or settings.APP_ENV,
        release=settings.SENTRY_RELEASE
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA"),
        send_default_pii=False,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
    )
from app.routers import (
    admin,
    auth,
    bookmarks,
    categories,
    chat,
    claims,
    comment_likes,
    comments,
    discovery,
    dish_lists,
    dishes,
    dishes_social,
    feed,
    feedback,
    follows,
    ghostwriter,
    images,
    likes,
    menus,
    notifications,
    owner_content,
    owner_dishes,
    owner_preferences,
    posts,
    ratings,
    reports,
    restaurants,
    reviews,
    safety,
    search,
    trending,
    user_preferences,
    users,
    want_to_try,
)


@asynccontextmanager
async def production_lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup
    uploads_parent = os.path.dirname(os.path.dirname(__file__))
    os.makedirs(os.path.join(uploads_parent, "uploads"), exist_ok=True)

    # Auto-create tables in development only. In production / staging
    # the schema is owned by Alembic — running ``create_all`` there
    # silently masks model/migration drift (creates tables that no
    # migration knows about).
    env = settings.APP_ENV.strip().lower()
    if env in {"development", "test"}:
        from app.database import Base
        import app.models  # noqa: F401 - ensure all models are imported
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
            await conn.run_sync(Base.metadata.create_all)

    # Async-job worker drains the ``async_job`` queue (re-embed +
    # sentiment for reviews). One coroutine per uvicorn worker; the
    # ``UPDATE ... FOR UPDATE SKIP LOCKED`` claim inside the loop
    # guarantees at-most-one process picks any given job. Toggleable
    # so tests and one-shot scripts run without it.
    worker_task: asyncio.Task[None] | None = None
    worker_stop_event: asyncio.Event | None = None
    if settings.ASYNC_JOB_WORKER_ENABLED:
        worker_stop_event = asyncio.Event()
        worker_task = asyncio.create_task(
            run_worker_loop(worker_stop_event), name="async_job_worker"
        )

    try:
        yield
    finally:
        # Shutdown — signal the worker to exit and give it a moment to
        # finish the in-flight job. The DB cascade on ``async_job``
        # plus the retry logic mean a hard kill is recoverable, but a
        # graceful drain keeps the next request snappier.
        if worker_stop_event is not None and worker_task is not None:
            worker_stop_event.set()
            try:
                await asyncio.wait_for(worker_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "async_job worker did not stop within 5s; cancelling"
                )
                worker_task.cancel()
        await engine.dispose()


def create_app(
    *,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Build the FastAPI application. Override *lifespan* for tests."""
    selected_lifespan = (
        lifespan if lifespan is not None else production_lifespan
    )
    application = FastAPI(
        title="Palato API",
        description=(
            "Food review platform where users review individual dishes"
        ),
        version="0.1.0",
        lifespan=selected_lifespan,
    )

    application.state.limiter = limiter
    application.add_exception_handler(
        RateLimitExceeded, _rate_limit_exceeded_handler
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(SecurityHeadersMiddleware)

    application.include_router(auth.router)
    # Specific /api/users/me/* paths from the legacy reviews router must be
    # registered BEFORE users.router so that its parametrized
    # `/{id_or_handle}/reviews` doesn't shadow them.
    application.include_router(reviews.router)
    application.include_router(users.router)
    application.include_router(follows.router)
    application.include_router(safety.router)
    application.include_router(likes.router)
    application.include_router(comments.router)
    application.include_router(comment_likes.router)
    application.include_router(notifications.router)
    application.include_router(bookmarks.router)
    application.include_router(want_to_try.router)
    application.include_router(reports.router)
    application.include_router(feed.router)
    application.include_router(search.router)
    application.include_router(trending.router)
    application.include_router(posts.router)
    application.include_router(chat.router)
    application.include_router(categories.router)
    application.include_router(restaurants.router)
    # discovery.router debe ir ANTES que dishes.router porque sus paths
    # específicos (/api/dishes/discover, /api/dishes/duel) son matcheados
    # por la ruta paramétrica /api/dishes/{dish_id} si esta se registra primero.
    application.include_router(discovery.router)
    application.include_router(dish_lists.router)
    application.include_router(dishes.router)
    application.include_router(dishes_social.router)
    application.include_router(ratings.router)
    application.include_router(feedback.router)
    application.include_router(images.router)
    application.include_router(menus.router)
    application.include_router(admin.router)
    application.include_router(claims.router)
    application.include_router(owner_content.router)
    application.include_router(owner_dishes.router)
    application.include_router(owner_preferences.router)
    application.include_router(user_preferences.router)
    application.include_router(ghostwriter.router)

    uploads_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "uploads",
    )
    os.makedirs(uploads_dir, exist_ok=True)
    application.mount(
        "/uploads",
        StaticFiles(directory=uploads_dir),
        name="uploads",
    )

    @application.get("/api/health")
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
