from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.main import create_app


@asynccontextmanager
async def lifespan_test(app: FastAPI) -> AsyncIterator[None]:
    """No DB startup; use for API smoke tests."""
    yield


@pytest.fixture
def app() -> FastAPI:
    return create_app(lifespan=lifespan_test)


@pytest.fixture
async def async_client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        yield client
