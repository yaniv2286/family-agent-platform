import uuid

import pytest
from sqlalchemy import select

from database import MemoryFact, SessionLocal, User
from memory_layer import MemoryCategory, user_memory


async def _child():
    async with SessionLocal() as db:
        child = User(name=f"ML-{uuid.uuid4().hex[:8]}", role="child")
        db.add(child)
        await db.commit()
        await db.refresh(child)
        return child


@pytest.mark.asyncio
async def test_store_and_retrieve_insights():
    child = await _child()
    insights = [
        {"category": MemoryCategory.SKILL, "key": "subtraction", "value": " mastered subtraction up to 20", "confidence": 0.95},
        {"category": MemoryCategory.PREFERENCE, "key": "short_answers", "value": "prefers short explanations", "confidence": 0.8},
    ]
    stored = await user_memory.store_insights(child.id, "koko_math", insights)
    assert len(stored) == 2

    ctx = await user_memory.get_context(child.id, "koko_math")
    assert ctx["user_id"] == child.id
    assert ctx["profile"]["name"] == child.name
    assert len(ctx["skills"]) == 1
    assert ctx["skills"][0]["key"] == "subtraction"
    assert len(ctx["preferences"]) == 1


@pytest.mark.asyncio
async def test_insight_update_replaces_old_value():
    child = await _child()
    await user_memory.store_insights(child.id, "koko_math", [
        {"category": MemoryCategory.SKILL, "key": "subtraction", "value": "level 1", "confidence": 0.5},
    ])
    await user_memory.store_insights(child.id, "koko_math", [
        {"category": MemoryCategory.SKILL, "key": "subtraction", "value": "level 2", "confidence": 0.9},
    ])

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(MemoryFact).where(
                MemoryFact.child_id == child.id,
                MemoryFact.category == MemoryCategory.SKILL,
                MemoryFact.key == "subtraction",
            )
        )).scalars().all()

    active = [r for r in rows if r.active == 1]
    inactive = [r for r in rows if r.active == 0]
    assert len(active) == 1
    assert len(inactive) == 1
    assert active[0].value == "level 2"


@pytest.mark.asyncio
async def test_add_episode_and_context():
    child = await _child()
    fact = await user_memory.add_episode(
        child.id, "coach", "logged_workout",
        {"workout_type": "walk", "status": "completed"},
    )
    assert fact is not None
    assert fact.category == MemoryCategory.EPISODE

    ctx = await user_memory.get_context(child.id, "coach")
    assert len(ctx["recent_episodes"]) == 1
    assert ctx["recent_episodes"][0]["value"]["type"] == "logged_workout"


@pytest.mark.asyncio
async def test_get_context_returns_none_profile_for_missing_user():
    ctx = await user_memory.get_context(999999, "coach")
    assert ctx["profile"] is None
    assert ctx["summary"] == "No user profile found."
