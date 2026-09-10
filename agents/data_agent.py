"""Data Steward agent: read-only data and memory queries for the mesh.

This agent is the single place where other agents ask questions about the
family's data. It never writes to the database directly (it may only add
memory facts through memory_manager when summarizing).

Mesh intents:
  - data.get_context           -> return prioritized memory context for a child
  - data.query_workouts        -> workout summary for a child
  - data.query_learning        -> learning summary for a child
"""
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_mesh import agent_mesh
from database import (
    ChatHistory,
    MemoryFact,
    SessionLocal,
    StudentProfile,
    TutorHistorySessionLocal,
    User,
    WorkoutPerformance,
    WorkoutPlan,
    get_db,
)
from memory_manager import memory_manager

router = APIRouter(prefix="/api/data", tags=["Data"])


class ContextRequest(BaseModel):
    child_id: int
    domain: Optional[str] = None


class WorkoutSummaryRequest(BaseModel):
    child_id: int
    days: int = 30


class LearningSummaryRequest(BaseModel):
    child_id: int


async def _child_by_id(child_id: int, db: AsyncSession) -> User:
    user = await db.get(User, child_id)
    if not user:
        raise HTTPException(404, "child not found")
    return user


async def handle_get_context(event, payload) -> Dict[str, Any]:
    """Return prioritized memory context for a child."""
    child_id = int(payload.get("child_id", event.child_id if event else 0))
    domain = payload.get("domain")
    return await memory_manager.get_context(child_id, domain)


async def handle_query_workouts(event, payload) -> Dict[str, Any]:
    """Return a read-only workout summary for a child."""
    child_id = int(payload.get("child_id", event.child_id if event else 0))
    days = int(payload.get("days", 30))
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with SessionLocal() as db:
        user = await db.get(User, child_id)
        if not user:
            return {"error": "child not found"}
        plans = (await db.execute(
            select(WorkoutPlan).where(WorkoutPlan.user_id == child_id)
        )).scalars().all()
        performances = (await db.execute(
            select(WorkoutPerformance)
            .where(
                WorkoutPerformance.user_id == child_id,
                WorkoutPerformance.completed_at >= cutoff,
            )
            .order_by(WorkoutPerformance.completed_at.desc())
        )).scalars().all()
        return {
            "child_id": child_id,
            "child_name": user.name,
            "period_days": days,
            "planned_total": len(plans),
            "completed_total": sum(1 for p in plans if p.status == "completed"),
            "recent_sessions": [
                {
                    "id": p.id,
                    "completed_at": p.completed_at.isoformat() if p.completed_at else None,
                    "duration_minutes": p.duration_minutes,
                    "rpe": p.perceived_exertion,
                    "fatigue": p.fatigue,
                    "pain": p.pain,
                }
                for p in performances
            ],
        }


async def handle_query_learning(event, payload) -> Dict[str, Any]:
    """Return a read-only learning summary for a child."""
    child_id = int(payload.get("child_id", event.child_id if event else 0))
    async with SessionLocal() as db:
        user = await db.get(User, child_id)
        if not user:
            return {"error": "child not found"}
    child_name = user.name
    subjects = ("math", "english")
    summary = {}
    async with TutorHistorySessionLocal() as history_db:
        for subject in subjects:
            profile = (await history_db.execute(
                select(StudentProfile).where(
                    StudentProfile.child_name == child_name,
                    StudentProfile.subject == subject,
                )
            )).scalar_one_or_none()
            messages = (await history_db.execute(
                select(func.count(ChatHistory.id)).where(
                    ChatHistory.child_name == child_name,
                    ChatHistory.subject == subject,
                    ChatHistory.role == "user",
                )
            )).scalar() or 0
            summary[subject] = {
                "profile_summary": profile.profile_summary if profile else None,
                "total_points": profile.total_points if profile else 0,
                "message_count": messages,
            }
    return {"child_id": child_id, "child_name": child_name, "subjects": summary}


agent_mesh.on_ask("data", "get_context", handle_get_context)
agent_mesh.on_ask("data", "query_workouts", handle_query_workouts)
agent_mesh.on_ask("data", "query_learning", handle_query_learning)


@router.post("/context")
async def get_context(data: ContextRequest):
    return await memory_manager.get_context(data.child_id, data.domain)


@router.post("/workouts")
async def query_workouts(data: WorkoutSummaryRequest):
    return await handle_query_workouts(None, data.model_dump())


@router.post("/learning")
async def query_learning(data: LearningSummaryRequest):
    return await handle_query_learning(None, data.model_dump())


@router.get("/facts/{child_id}")
async def list_facts(child_id: int, domain: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    await _child_by_id(child_id, db)
    facts = await memory_manager.get_facts_for_child(child_id, domain)
    return [
        {
            "id": f.id,
            "domain": f.domain,
            "category": f.category,
            "key": f.key,
            "value": json.loads(f.value) if f.value else None,
            "confidence": f.confidence,
            "source_agent": f.source_agent,
            "created_at": f.created_at,
        }
        for f in facts
    ]
