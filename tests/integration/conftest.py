import httpx
import pytest
from fastapi import FastAPI

from app.main import create_app


@pytest.fixture
def integration_app() -> FastAPI:
    return create_app()


@pytest.fixture
async def async_client_integration(integration_app: FastAPI):
    transport = httpx.ASGITransport(app=integration_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        yield client
