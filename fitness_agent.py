import json
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import HealthLog, UserFitnessProfile, get_db

router = APIRouter(prefix="/api/fitness", tags=["Fitness Agent"])

DEFAULT_EQUIPMENT = [
    "balance_board",
    "balance_pad",
    "fitball",
    "pull_up_bar",
    "resistance_bands",
    "pilates_mat",
    "trx",
]

DEFAULT_HEALTH_CONSTRAINTS = [
    "tilt_test_sensitivity",
    "avoid_sudden_orthostatic_drops",
    "low_impact_strength",
]

DEFAULT_GOAL_TYPE = "hypertrophy_body_recomposition_fat_loss"

DEFAULT_SUPPLEMENT_STACK = ["creatine", "whey_protein"]


class WorkoutLogRequest(BaseModel):
    user_id: int
    workout_type: str
    exercises: List = []
    status: str
    notes: str = ""


@router.post("/log")
async def log_workout(data: WorkoutLogRequest, db: AsyncSession = Depends(get_db)):
    try:
        health_log = HealthLog(
            user_id=data.user_id,
            workout_type=data.workout_type,
            exercises_json=json.dumps(data.exercises, ensure_ascii=False),
            status=data.status,
            notes=data.notes,
        )
        db.add(health_log)
        await db.commit()
        await db.refresh(health_log)
        return {"status": "success", "message": "Workout logged and synced with cross-agent state."}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/profile/{user_id}")
async def get_fitness_profile(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(UserFitnessProfile).where(UserFitnessProfile.user_id == user_id)
    )
    row = result.scalar_one_or_none()

    if not row:
        return {
            "equipment": DEFAULT_EQUIPMENT,
            "health_constraints": DEFAULT_HEALTH_CONSTRAINTS,
            "goal_type": DEFAULT_GOAL_TYPE,
            "supplement_stack": DEFAULT_SUPPLEMENT_STACK,
        }

    return {
        "equipment": json.loads(row.equipment),
        "health_constraints": json.loads(row.health_constraints),
        "goal_type": row.goal_type,
        "supplement_stack": json.loads(row.supplement_stack),
    }
