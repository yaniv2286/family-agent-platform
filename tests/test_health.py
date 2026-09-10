import pytest


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"]
    assert data["checks"]["main_db"] is True
    assert data["checks"]["tutor_history_db"] is True


@pytest.mark.asyncio
async def test_health_endpoint_does_not_require_pin(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
