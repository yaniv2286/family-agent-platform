import json
import uuid

import pytest
from sqlalchemy import select

from database import (
    AgentAction,
    AgentEvent,
    MemoryFact,
    ParentDirective,
    SessionLocal,
    User,
    add_parent_feedback,
)
from memory_manager import memory_manager
from policy_engine import policy_engine

APP_PIN = "test-pin-1234"


async def _child():
    async with SessionLocal() as db:
        child = User(name=f"Collab-{uuid.uuid4().hex[:8]}", role="child")
        db.add(child)
        await db.commit()
        await db.refresh(child)
        return child


@pytest.mark.asyncio
async def test_fitness_completion_creates_nutrition_memory_and_audit(client):
    child = await _child()
    response = await client.post(
        "/api/fitness/log", headers={"x-app-pin": APP_PIN},
        json={"user_id": child.id, "workout_type": "walk", "exercises": [],
              "status": "completed", "notes": ""},
    )
    assert response.status_code == 200
    async with SessionLocal() as db:
        fact = (await db.execute(select(MemoryFact).where(
            MemoryFact.child_id == child.id, MemoryFact.domain == "nutrition"
        ))).scalar_one()
        event = (await db.execute(select(AgentEvent).where(
            AgentEvent.child_id == child.id
        ))).scalar_one()
    assert "balanced meal" in fact.value
    assert event.status == "processed"
    assert json.loads(fact.evidence_json)[0]["event_id"] == event.id


@pytest.mark.asyncio
async def test_directive_lifecycle_and_tutor_memory_retrieval(client):
    child = await _child()
    await add_parent_feedback(child.name, "math", "practice multiplication gently")
    directives = await memory_manager.get_active_directives(child.name, "math")
    assert [d.message for d in directives] == ["practice multiplication gently"]
    response = await client.patch(
        f"/api/collaboration/directives/{directives[0].id}",
        headers={"x-app-pin": APP_PIN}, json={"status": "completed"},
    )
    assert response.status_code == 200
    assert await memory_manager.get_active_directives(child.name, "math") == []


@pytest.mark.asyncio
async def test_directive_cancel_lifecycle(client):
    child = await _child()
    await add_parent_feedback(child.name, "english", "review vocabulary")
    directives = await memory_manager.get_active_directives(child.name, "english")
    assert len(directives) == 1
    response = await client.patch(
        f"/api/collaboration/directives/{directives[0].id}",
        headers={"x-app-pin": APP_PIN}, json={"status": "cancelled"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cancelled"
    assert await memory_manager.get_active_directives(child.name, "english") == []


@pytest.mark.asyncio
async def test_directive_lifecycle_rejects_invalid_status(client):
    child = await _child()
    await add_parent_feedback(child.name, "math", "do addition")
    directives = await memory_manager.get_active_directives(child.name, "math")
    response = await client.patch(
        f"/api/collaboration/directives/{directives[0].id}",
        headers={"x-app-pin": APP_PIN}, json={"status": "invalid"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_directive_double_complete_is_conflict(client):
    child = await _child()
    await add_parent_feedback(child.name, "math", "drill tables")
    directives = await memory_manager.get_active_directives(child.name, "math")
    did = directives[0].id
    first = await client.patch(
        f"/api/collaboration/directives/{did}",
        headers={"x-app-pin": APP_PIN}, json={"status": "completed"},
    )
    assert first.status_code == 200
    second = await client.patch(
        f"/api/collaboration/directives/{did}",
        headers={"x-app-pin": APP_PIN}, json={"status": "completed"},
    )
    assert second.status_code == 409


def test_policy_precedence():
    resolved = policy_engine.resolve([
        {"category": "preference", "content": "sweet snack"},
        {"category": "goal", "content": "gain strength"},
        {"category": "parent_directive", "content": "eat together"},
        {"category": "safety_constraint", "content": "allergy"},
    ])
    assert [item["category"] for item in resolved] == [
        "safety_constraint", "parent_directive", "goal", "preference"
    ]


@pytest.mark.asyncio
async def test_memory_is_isolated_between_children():
    first, second = await _child(), await _child()
    await memory_manager.add_fact(first.id, first.id, "learning", "goal", "topic", "fractions")
    await memory_manager.add_fact(second.id, second.id, "learning", "goal", "topic", "spelling")
    first_facts = await memory_manager.get_facts(first.id, first.id, "learning")
    second_facts = await memory_manager.get_facts(second.id, second.id, "learning")
    assert [fact.value for fact in first_facts] == ["fractions"]
    assert [fact.value for fact in second_facts] == ["spelling"]


@pytest.mark.asyncio
async def test_context_is_isolated_between_children():
    first, second = await _child(), await _child()
    await memory_manager.add_fact(first.id, first.id, "nutrition", "recommendation", "snack", "apple")
    await add_parent_feedback(first.name, None, "first only directive")
    first_ctx = await memory_manager.get_context(first.id)
    second_ctx = await memory_manager.get_context(second.id)
    first_contents = [item["content"] for item in first_ctx]
    second_contents = [item["content"] for item in second_ctx]
    assert "apple" in first_contents
    assert "apple" not in second_contents


@pytest.mark.asyncio
async def test_context_endpoint_response_contract(client):
    child = await _child()
    await memory_manager.add_fact(child.id, child.id, "fitness", "goal", "k", "run daily")
    response = await client.get(
        f"/api/collaboration/context/{child.id}",
        headers={"x-app-pin": APP_PIN},
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    item = data[0]
    assert "kind" in item
    assert "category" in item
    assert "content" in item
    assert "priority" in item
    assert "confidence" in item


@pytest.mark.asyncio
async def test_directives_endpoint_response_contract(client):
    child = await _child()
    await add_parent_feedback(child.name, "math", "focus on fractions")
    response = await client.get(
        f"/api/collaboration/directives/{child.id}",
        headers={"x-app-pin": APP_PIN},
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    d = data[0]
    for key in ("id", "message", "subject", "priority", "scope", "status", "child_name", "created_at"):
        assert key in d, f"missing key {key}"
    assert d["status"] == "active"
    assert d["message"] == "focus on fractions"


@pytest.mark.asyncio
async def test_events_endpoint_response_contract(client):
    child = await _child()
    await client.post(
        "/api/fitness/log", headers={"x-app-pin": APP_PIN},
        json={"user_id": child.id, "workout_type": "run", "exercises": [],
              "status": "completed", "notes": ""},
    )
    response = await client.get(
        f"/api/collaboration/events/{child.id}",
        headers={"x-app-pin": APP_PIN},
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    ev = data[0]
    for key in ("id", "type", "source_agent", "payload", "status", "created_at"):
        assert key in ev, f"missing key {key}"
    assert ev["type"] == "fitness.workout_completed"
    assert ev["source_agent"] == "fitness"


@pytest.mark.asyncio
async def test_actions_endpoint_response_contract(client):
    child = await _child()
    await client.post(
        "/api/fitness/log", headers={"x-app-pin": APP_PIN},
        json={"user_id": child.id, "workout_type": "swim", "exercises": [],
              "status": "completed", "notes": ""},
    )
    response = await client.get(
        f"/api/collaboration/actions/{child.id}",
        headers={"x-app-pin": APP_PIN},
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    act = data[0]
    for key in ("id", "event_id", "agent", "action", "status", "detail", "created_at"):
        assert key in act, f"missing key {key}"
    assert act["agent"] == "nutrition"
    assert act["status"] == "completed"


@pytest.mark.asyncio
async def test_incomplete_workout_does_not_publish_event(client):
    child = await _child()
    response = await client.post(
        "/api/fitness/log", headers={"x-app-pin": APP_PIN},
        json={"user_id": child.id, "workout_type": "stretch", "exercises": [],
              "status": "planned", "notes": ""},
    )
    assert response.status_code == 200
    async with SessionLocal() as db:
        events = (await db.execute(
            select(AgentEvent).where(AgentEvent.child_id == child.id)
        )).scalars().all()
    assert len(events) == 0


@pytest.mark.asyncio
async def test_directives_are_isolated_between_children():
    first, second = await _child(), await _child()
    await add_parent_feedback(first.name, "math", "first child directive")
    await add_parent_feedback(second.name, "math", "second child directive")
    first_dirs = await memory_manager.get_active_directives(child_name=first.name)
    second_dirs = await memory_manager.get_active_directives(child_name=second.name)
    first_msgs = [d.message for d in first_dirs]
    second_msgs = [d.message for d in second_dirs]
    assert "first child directive" in first_msgs
    assert "second child directive" not in first_msgs
    assert "second child directive" in second_msgs
    assert "first child directive" not in second_msgs
