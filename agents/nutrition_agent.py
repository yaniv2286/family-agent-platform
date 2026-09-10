"""Full Nutrition Agent on the agent mesh.

Originally this was only an event-only collaborator triggered by fitness
workouts. It is now a first-class agent that can:
  - subscribe to fitness.workout_completed events
  - answer mesh ask() requests for post-workout recommendations
  - expose direct API endpoints under /api/nutrition
"""
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_mesh import agent_mesh
from database import SessionLocal, User, UserFitnessProfile, get_db
from memory_manager import memory_manager

router = APIRouter(prefix="/api/nutrition", tags=["Nutrition"])


class PostWorkoutRequest(BaseModel):
    user_id: int
    duration_minutes: int = 30
    constraints: List[str] = []


async def _health_constraints(user_id: int) -> List[str]:
    async with SessionLocal() as db:
        profile = (await db.execute(
            select(UserFitnessProfile).where(UserFitnessProfile.user_id == user_id)
        )).scalar_one_or_none()
        if not profile:
            return []
        try:
            return json.loads(profile.health_constraints) or []
        except (TypeError, ValueError):
            return [profile.health_constraints] if profile.health_constraints else []


def _build_recommendation(constraints: List[str]) -> str:
    base = (
        "After activity, drink water and choose a normal balanced meal or snack with "
        "carbohydrate and protein when hungry."
    )
    if constraints:
        base += " Follow known health constraints and a parent or qualified clinician's individual guidance."
    return base


async def handle_workout_completed(event, payload):
    """Event subscriber: write a post-workout nutrition memory fact."""
    constraints = await _health_constraints(event.child_id)
    recommendation = _build_recommendation(constraints)
    await memory_manager.add_fact(
        user_id=event.user_id,
        child_id=event.child_id,
        domain="nutrition",
        category="recommendation",
        key="post_workout_recommendation",
        value=recommendation,
        confidence=0.8,
        evidence=[{
            "event_id": event.id,
            "workout": payload.get("workout_type"),
            "health_constraints": constraints,
        }],
        source_agent="nutrition",
    )


async def handle_post_workout_recommendation(event, payload):
    """Ask handler: return a recommendation for a specific workout."""
    user_id = event.child_id
    constraints = await _health_constraints(user_id)
    return {
        "recommendation": _build_recommendation(constraints),
        "constraints": constraints,
        "source_agent": "nutrition",
    }


# Register with the agent mesh.
agent_mesh.on_event("nutrition", "fitness.workout_completed", handle_workout_completed)
agent_mesh.on_ask("nutrition", "post_workout_recommendation", handle_post_workout_recommendation)


@router.post("/recommend/post-workout")
async def recommend_post_workout(data: PostWorkoutRequest, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, data.user_id)
    if not user:
        raise HTTPException(404, "user not found")
    constraints = await _health_constraints(data.user_id)
    recommendation = _build_recommendation(constraints)
    await memory_manager.add_fact(
        user_id=data.user_id,
        child_id=data.user_id,
        domain="nutrition",
        category="recommendation",
        key="post_workout_recommendation",
        value=recommendation,
        confidence=0.8,
        evidence=[{"duration_minutes": data.duration_minutes, "constraints": constraints}],
        source_agent="nutrition",
        db=db,
    )
    await db.commit()
    return {"recommendation": recommendation, "constraints": constraints}
