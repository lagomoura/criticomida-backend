"""Smoke test: health endpoint without database lifespan."""

import pytest


@pytest.mark.asyncio
async def test_api_health_returns_ok(async_client):
    response = await async_client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
