import json

from sqlalchemy import select

from agent_registry import agent_registry
from database import SessionLocal, UserFitnessProfile
from memory_manager import memory_manager


async def handle_workout_completed(event, payload):
    constraints = []
    async with SessionLocal() as db:
        profile = (await db.execute(
            select(UserFitnessProfile).where(UserFitnessProfile.user_id == event.child_id)
        )).scalar_one_or_none()
        if profile:
            try:
                constraints = json.loads(profile.health_constraints)
            except (TypeError, ValueError):
                constraints = [profile.health_constraints]

    recommendation = (
        "After activity, drink water and choose a normal balanced meal or snack with "
        "carbohydrate and protein when hungry. Follow known health constraints and a "
        "parent or qualified clinician's individual guidance."
    )
    await memory_manager.add_fact(
        user_id=event.user_id, child_id=event.child_id, domain="nutrition",
        category="recommendation", key="post_workout_recommendation", value=recommendation,
        confidence=0.8, evidence=[{"event_id": event.id, "workout": payload.get("workout_type"),
                                  "health_constraints": constraints}], source_agent="nutrition",
    )


handle_workout_completed.agent_name = "nutrition"
agent_registry.register("fitness.workout_completed", handle_workout_completed)
