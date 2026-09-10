import pytest
from sqlalchemy import select

from agent_mesh import AgentMesh, AgentMeshTimeout
from database import AgentAction, AgentEvent, SessionLocal, User

APP_PIN = "test-pin-1234"


@pytest.fixture
def fresh_mesh():
    """Return a private AgentMesh instance isolated from the global one."""
    return AgentMesh()


@pytest.mark.asyncio
async def test_mesh_ask_returns_handler_result(fresh_mesh):
    async def echo_handler(event, payload):
        return {"received": payload}

    fresh_mesh.on_ask("test_agent", "echo", echo_handler)
    response = await fresh_mesh.ask(
        to_agent="test_agent",
        intent="echo",
        user_id=1,
        child_id=1,
        source_agent="orchestrator",
        payload={"hello": "world"},
    )
    assert response["ok"] is True
    assert response["result"]["received"]["hello"] == "world"


@pytest.mark.asyncio
async def test_mesh_ask_unknown_intent_returns_error(fresh_mesh):
    response = await fresh_mesh.ask(
        to_agent="test_agent",
        intent="missing",
        user_id=1,
        child_id=1,
        source_agent="orchestrator",
    )
    assert response["ok"] is False
    assert "no ask handler" in response["error"]


@pytest.mark.asyncio
async def test_mesh_ask_writes_audit_records(fresh_mesh):
    async def handler(event, payload):
        return {"ok": True}

    fresh_mesh.on_ask("test_agent", "audit", handler)
    response = await fresh_mesh.ask(
        to_agent="test_agent",
        intent="audit",
        user_id=1,
        child_id=1,
        source_agent="orchestrator",
    )
    assert response["ok"] is True
    async with SessionLocal() as db:
        event = (await db.execute(
            select(AgentEvent).where(AgentEvent.event_type == "ask.test_agent.audit")
        )).scalar_one_or_none()
        action = (await db.execute(
            select(AgentAction).where(AgentAction.action_type == "ask.audit")
        )).scalar_one_or_none()
    assert event is not None
    assert event.status == "processed"
    assert action is not None
    assert action.status == "completed"


@pytest.mark.asyncio
async def test_mesh_ask_timeout_is_recorded(fresh_mesh):
    async def slow_handler(event, payload):
        import asyncio
        await asyncio.sleep(10)
        return {}

    fresh_mesh.on_ask("test_agent", "slow", slow_handler)
    with pytest.raises(AgentMeshTimeout):
        await fresh_mesh.ask(
            to_agent="test_agent",
            intent="slow",
            user_id=1,
            child_id=1,
            source_agent="orchestrator",
            timeout=0.1,
        )
    async with SessionLocal() as db:
        action = (await db.execute(
            select(AgentAction).where(AgentAction.action_type == "ask.slow")
        )).scalar_one_or_none()
    assert action is not None
    assert action.status == "failed"


@pytest.mark.asyncio
async def test_mesh_publish_invokes_event_handler(fresh_mesh):
    calls = []

    async def handler(event, payload):
        calls.append(payload)

    fresh_mesh.on_event("test_agent", "thing.happened", handler)
    event = await fresh_mesh.publish(
        event_type="thing.happened",
        user_id=1,
        child_id=1,
        source_agent="orchestrator",
        payload={"x": 1},
    )
    assert event.status == "processed"
    assert len(calls) == 1
    assert calls[0]["x"] == 1


@pytest.mark.asyncio
async def test_nutrition_ask_post_workout_recommendation(client):
    async with SessionLocal() as db:
        child = User(name="NutritionMeshChild", role="child")
        db.add(child)
        await db.commit()
        await db.refresh(child)

    from agent_mesh import agent_mesh
    response = await agent_mesh.ask(
        to_agent="nutrition",
        intent="post_workout_recommendation",
        user_id=child.id,
        child_id=child.id,
        source_agent="fitness",
        payload={"duration_minutes": 30},
    )
    assert response["ok"] is True
    assert "balanced meal" in response["result"]["recommendation"]


@pytest.mark.asyncio
async def test_messenger_mesh_send_message_uses_telegram(monkeypatch):
    from agents import messenger_agent
    from agent_mesh import agent_mesh

    send_mock = messenger_agent.send_telegram_message
    calls = []

    async def fake_send(text):
        calls.append(text)
        return True

    monkeypatch.setattr(messenger_agent, "send_telegram_message", fake_send)
    response = await agent_mesh.ask(
        to_agent="messenger",
        intent="send_message",
        user_id=1,
        child_id=1,
        source_agent="orchestrator",
        payload={"channel": "telegram", "text": "hello from mesh"},
    )
    assert response["ok"] is True
    assert response["result"]["sent"] is True
    assert calls == ["hello from mesh"]


@pytest.mark.asyncio
async def test_orchestrator_run_intent_endpoint(client):
    response = await client.post(
        "/api/orchestrator/intents/daily_summary",
        headers={"x-app-pin": APP_PIN},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["intent"] == "daily_summary"
    assert "result" in data


@pytest.mark.asyncio
async def test_data_agent_learning_endpoint(client):
    async with SessionLocal() as db:
        child = User(name="DataAgentChild", role="child")
        db.add(child)
        await db.commit()
        await db.refresh(child)

    response = await client.post(
        "/api/data/learning",
        headers={"x-app-pin": APP_PIN},
        json={"child_id": child.id},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["child_id"] == child.id
    assert "subjects" in data
    assert "math" in data["subjects"]
    assert "english" in data["subjects"]


@pytest.mark.asyncio
async def test_data_agent_get_context_ask(client):
    async with SessionLocal() as db:
        child = User(name="DataAgentContextChild", role="child")
        db.add(child)
        await db.commit()
        await db.refresh(child)

    from agent_mesh import agent_mesh
    response = await agent_mesh.ask(
        to_agent="data",
        intent="get_context",
        user_id=child.id,
        child_id=child.id,
        source_agent="orchestrator",
        payload={"domain": "learning"},
    )
    assert response["ok"] is True
    assert isinstance(response["result"], list)


@pytest.mark.asyncio
async def test_nutrition_direct_endpoint_creates_fact(client):
    async with SessionLocal() as db:
        child = User(name="NutritionEndpointChild", role="child")
        db.add(child)
        await db.commit()
        await db.refresh(child)

    response = await client.post(
        "/api/nutrition/recommend/post-workout",
        headers={"x-app-pin": APP_PIN},
        json={"user_id": child.id, "duration_minutes": 20},
    )
    assert response.status_code == 200
    data = response.json()
    assert "recommendation" in data
