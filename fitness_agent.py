import json
import random
import re
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import (BodyMetric, Exercise, HealthLog, PlannedWorkoutExercise, ProgressionHistory,
                      TinyHabit, TinyHabitCompletion, User, UserFitnessProfile,
                      WorkoutPerformance, WorkoutPlan, WorkoutReadiness, WorkoutSet, get_db)
from event_bus import event_bus
from memory_manager import memory_manager
from memory_layer import MemoryCategory, user_memory
from rbac import Role, require_permission
import nutrition_agent  # register collaboration handler

AGENT_ID = "fitness"


def read_access(request: Request) -> str:
    """Enforce the documented RBAC matrix for reads of Coach data."""
    return require_permission(request, AGENT_ID, "read")


def write_access(request: Request) -> str:
    """Enforce the documented RBAC matrix for Coach mutations."""
    return require_permission(request, AGENT_ID, "command")


router = APIRouter(prefix="/api/fitness", tags=["Coach"])
DEFAULT_EQUIPMENT = ["none", "balance_board", "balance_pad", "fitball", "pull_up_bar", "resistance_bands", "pilates_mat", "trx"]
DEFAULT_HEALTH_CONSTRAINTS = ["tilt_test_sensitivity", "avoid_sudden_orthostatic_drops", "low_impact_strength"]
DEFAULT_GOAL_TYPE = "hypertrophy_body_recomposition_fat_loss"
DEFAULT_SUPPLEMENT_STACK = ["creatine", "whey_protein"]
DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def _loads(value, fallback=None):
    try: return json.loads(value or "[]")
    except (TypeError, ValueError): return fallback or []


def _normalize_video_url(url: str) -> str:
    """Return a short youtu.be link for a YouTube URL.

    Supports:
      - https://www.youtube.com/watch?v=<id>
      - https://youtube.com/watch?v=<id>
      - https://www.youtube.com/shorts/<id>
      - https://youtube.com/shorts/<id>
      - https://youtu.be/<id> (returned as-is)
    """
    if not url:
        return url
    url = url.strip()
    if url.startswith("https://youtu.be/"):
        return url
    watch_match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", url)
    if watch_match:
        return f"https://youtu.be/{watch_match.group(1)}"
    shorts_match = re.search(r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})", url)
    if shorts_match:
        return f"https://youtu.be/{shorts_match.group(1)}"
    return url


async def _exercises_to_avoid(db: AsyncSession, user_id: int, lookback_days: int = 30) -> set[int]:
    """Return exercise IDs from recent sessions the body did not tolerate.

    The thresholds mirror the `blocked` rule in complete_plan exactly, so any
    session that blocked progression also stops those movements being
    re-prescribed.
    """
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    rows = (await db.execute(
        select(WorkoutPerformance, WorkoutSet.exercise_id)
        .join(WorkoutSet, WorkoutSet.performance_id == WorkoutPerformance.id)
        .where(
            WorkoutPerformance.user_id == user_id,
            WorkoutPerformance.completed_at >= cutoff,
            ((WorkoutPerformance.pain > 0) | (WorkoutPerformance.perceived_exertion >= 9) | (WorkoutPerformance.fatigue >= 8)),
        )
    )).all()
    return {int(exercise_id) for _, exercise_id in rows if exercise_id}


async def _recent_comfortable_exercises(db: AsyncSession, user_id: int, lookback_days: int = 30, top_n: int = 5) -> List[int]:
    """Return exercise IDs that were completed comfortably (low RPE/fatigue/no pain)."""
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    rows = (await db.execute(
        select(WorkoutSet.exercise_id, func.count().label("cnt"))
        .join(WorkoutPerformance, WorkoutPerformance.id == WorkoutSet.performance_id)
        .where(
            WorkoutPerformance.user_id == user_id,
            WorkoutPerformance.completed_at >= cutoff,
            WorkoutPerformance.pain == 0,
            WorkoutPerformance.perceived_exertion <= 7,
            WorkoutPerformance.fatigue <= 5,
        )
        .group_by(WorkoutSet.exercise_id)
        .order_by(func.count().desc())
        .limit(top_n)
    )).all()
    return [int(exercise_id) for exercise_id, _ in rows]


async def _recently_used_exercises(db: AsyncSession, user_id: int, lookback_days: int = 7) -> set[int]:
    """Return exercise IDs already assigned in recent completed plans."""
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    rows = (await db.execute(
        select(PlannedWorkoutExercise.exercise_id)
        .join(WorkoutPlan, WorkoutPlan.id == PlannedWorkoutExercise.plan_id)
        .where(
            WorkoutPlan.user_id == user_id,
            WorkoutPlan.status == "completed",
            WorkoutPlan.completed_at >= cutoff,
        )
    )).scalars().all()
    return {int(exercise_id) for exercise_id in rows if exercise_id}


BASELINE_SETS = 2
BASELINE_REPS = 8
MAX_REPS = 15
MAX_SETS = 4
ADULT_PRESCRIPTIONS = {
    "beginner": {"sets": 3, "reps": 10, "target_rir": 3.0, "rest_seconds": 90},
    "intermediate": {"sets": 4, "reps": 10, "target_rir": 2.5, "rest_seconds": 120},
    "advanced": {"sets": 4, "reps": 12, "target_rir": 2.0, "rest_seconds": 120},
}
MUSCLE_VOLUME_TARGETS = {
    "beginner": {"weekly_sets_per_muscle": 8},
    "intermediate": {"weekly_sets_per_muscle": 10},
    "advanced": {"weekly_sets_per_muscle": 12},
}
MUSCLE_GROUP_ORDER = ("chest", "back", "legs", "shoulders", "arms", "glutes", "core", "balance", "cardio", "full_body")


def _adult_prescription(experience_level: str) -> dict:
    return ADULT_PRESCRIPTIONS.get(experience_level, ADULT_PRESCRIPTIONS["beginner"])


def _muscle_group_sort(exercise: Exercise) -> int:
    try:
        return MUSCLE_GROUP_ORDER.index(exercise.muscle_group)
    except ValueError:
        return len(MUSCLE_GROUP_ORDER)


async def _progression_targets(db: AsyncSession, user_id: int, exercise_ids: List[int]) -> dict[int, dict]:
    """Return per-exercise set/rep targets based on the last recorded session.

    This closes the loop on ProgressionHistory: an exercise the user handled
    comfortably (progression_allowed) earns exactly one small increase - reps
    first, then an extra set once reps top out. Anything else holds steady, and
    a blocked session steps back toward the baseline. Never more than one
    variable changes at a time.
    """
    if not exercise_ids:
        return {}
    rows = (await db.execute(
        select(WorkoutSet.exercise_id, WorkoutPerformance.progression_allowed,
               func.max(WorkoutSet.set_number).label("sets"), func.max(WorkoutSet.reps).label("reps"),
               func.max(WorkoutSet.resistance).label("resistance"), func.min(WorkoutSet.rir).label("rir"),
               WorkoutPerformance.completed_at)
        .join(WorkoutPerformance, WorkoutPerformance.id == WorkoutSet.performance_id)
        .where(WorkoutPerformance.user_id == user_id, WorkoutSet.exercise_id.in_(exercise_ids))
        .group_by(WorkoutSet.exercise_id, WorkoutPerformance.id)
        .order_by(WorkoutPerformance.completed_at.desc())
    )).all()
    targets: dict[int, dict] = {}
    for exercise_id, allowed, last_sets, last_reps, last_resistance, last_rir, _ in rows:
        if exercise_id is None or exercise_id in targets:
            continue  # rows are newest-first, so the first hit is the latest session
        sets = int(last_sets or BASELINE_SETS) or BASELINE_SETS
        reps = int(last_reps or BASELINE_REPS) or BASELINE_REPS
        resistance = float(last_resistance) if last_resistance is not None else None
        adjustment = "hold"
        if allowed:
            if resistance is not None and last_rir is not None and float(last_rir) <= 1:
                resistance = round(resistance * 0.95, 2)
                adjustment = "reduce_load"
            elif resistance is not None and last_rir is not None and float(last_rir) >= 2 and reps >= 10:
                resistance = round(resistance * 1.025, 2)
                adjustment = "increase_load"
            elif reps < MAX_REPS:
                reps += 1
                adjustment = "increase_reps"
            elif sets < MAX_SETS:
                sets, reps = sets + 1, BASELINE_REPS
                adjustment = "increase_sets"
        else:
            # Step back toward the baseline after a painful or maximal session.
            reps = max(5, min(reps, BASELINE_REPS))
            sets = max(1, min(sets, BASELINE_SETS))
            if resistance is not None:
                resistance = round(resistance * 0.95, 2)
            adjustment = "deload"
        targets[int(exercise_id)] = {"sets": sets, "reps": reps, "resistance": resistance,
                                      "progressed": adjustment.startswith("increase"),
                                      "adjustment": adjustment}
    return targets


def _next_training_date(training_days: List[str]) -> date:
    """Return the soonest configured training day, today included."""
    if not training_days:
        return date.today()
    wanted = {d.lower() for d in training_days} & DAYS
    if not wanted:
        return date.today()
    today = date.today()
    for offset in range(7):
        candidate = today + timedelta(days=offset)
        if candidate.strftime("%A").lower() in wanted:
            return candidate
    return today


async def _memory_notes_for_plan(user_id: int) -> List[str]:
    """Return short safety/preference notes from the unified memory layer."""
    try:
        context = await user_memory.get_context(user_id, "coach")
    except Exception:
        # Safety-relevant notes must never fail silently and unobserved.
        logger.exception("Could not load Coach memory context for plan notes", user_id=user_id)
        return []
    notes = []
    for item in context.get("preferences", []):
        value = str(item.get("value", "")).lower()
        if "pain" in value or "fatigue" in value or "dizzy" in value or "avoid" in value:
            notes.append(f"Memory note: {item['value']}")
    for item in context.get("behaviors", []):
        value = str(item.get("value", "")).lower()
        if any(k in value for k in ("fatigue", "exertion", "rpe")):
            notes.append(f"Memory note: {item['value']}")
    for item in context.get("relationship", []):
        notes.append(f"Memory note: {item['value']}")
    return notes[:3]


def _coach_greeting(user_id: int, exercises: List[dict]) -> str:
    """Return a personalized Coach greeting for the workout plan."""
    names = [e["name"] for e in exercises[:3]]
    if names:
        exercise_list = ", ".join(names)
        return f"Coach here! I picked these exercises just for you: {exercise_list}. Move with control, breathe, and stop if anything feels off. You've got this!"
    return "Coach here! Let's move with intention and keep everything pain-free."


def _workout_insights(data, plan_id, blocked):
    """Build heuristic memory insights from a completed workout."""
    insights = []
    if data.pain > 0:
        insights.append({
            "category": MemoryCategory.PREFERENCE,
            "key": "workout_pain_reported",
            "value": "Reported pain during a workout; future plans should emphasize recovery and avoid aggravating movements until cleared by a clinician.",
            "confidence": 1.0,
            "evidence": [{"rpe": data.perceived_exertion, "pain": data.pain, "notes": data.notes}],
        })
    if data.perceived_exertion >= 9:
        insights.append({
            "category": MemoryCategory.BEHAVIOR,
            "key": "very_high_exertion",
            "value": "Worked at maximum perceived exertion (RPE 9+). Consider shorter or easier sessions next time.",
            "confidence": 1.0,
            "evidence": [{"rpe": data.perceived_exertion, "duration": data.duration_minutes}],
        })
    if data.fatigue >= 8:
        insights.append({
            "category": MemoryCategory.BEHAVIOR,
            "key": "high_fatigue",
            "value": "Reported high fatigue. Monitor sleep/load and default to low-impact options until energy recovers.",
            "confidence": 1.0,
            "evidence": [{"fatigue": data.fatigue, "notes": data.notes}],
        })
    if not blocked and data.perceived_exertion <= 5 and data.fatigue <= 3:
        insights.append({
            "category": MemoryCategory.SKILL,
            "key": "comfortable_with_current_load",
            "value": "Handled the current workout load comfortably. Ready for a small progression when the next session feels similar.",
            "confidence": 0.85,
            "evidence": [{"rpe": data.perceived_exertion, "fatigue": data.fatigue, "duration": data.duration_minutes}],
        })
    if data.notes and len(data.notes.strip()) > 5:
        insights.append({
            "category": MemoryCategory.EPISODE,
            "key": "workout_note",
            "value": data.notes.strip()[:500],
            "confidence": 0.95,
            "evidence": [{"plan_id": plan_id}],
        })
    return insights


class ProfileUpdate(BaseModel):
    equipment: list[str] = Field(default_factory=list, max_length=40)
    health_constraints: list[str] = Field(default_factory=list, max_length=40)
    goals: list[str] = Field(default_factory=list, max_length=20)
    supplements: list[str] = Field(default_factory=list, max_length=40)
    preferred_duration_minutes: int = Field(30, ge=5, le=120)
    training_days: list[str] = Field(default_factory=list, max_length=7)
    training_mode: str = Field("family_fitness", pattern="^(family_fitness|adult_hypertrophy)$")
    age: Optional[int] = Field(None, ge=13, le=100)
    height_cm: Optional[float] = Field(None, ge=100, le=250)
    weight_kg: Optional[float] = Field(None, ge=30, le=350)
    experience_level: str = Field("beginner", pattern="^(beginner|intermediate|advanced)$")
    @field_validator("equipment", "health_constraints", "goals", "supplements")
    @classmethod
    def clean_lists(cls, value):
        result = [x.strip().lower() for x in value if isinstance(x, str) and x.strip()]
        if len(result) != len(value) or any(len(x) > 80 for x in result): raise ValueError("invalid list value")
        return list(dict.fromkeys(result))
    @field_validator("training_days")
    @classmethod
    def valid_days(cls, value):
        result = [x.lower() for x in value]
        if any(x not in DAYS for x in result): raise ValueError("invalid training day")
        return list(dict.fromkeys(result))


class SetInput(BaseModel):
    set_number: int = Field(ge=1, le=20)
    reps: Optional[int] = Field(None, ge=0, le=500)
    duration_seconds: Optional[int] = Field(None, ge=0, le=7200)
    resistance: Optional[float] = Field(None, ge=0, le=1000)
    rir: Optional[float] = Field(None, ge=0, le=10)
class ExercisePerformanceInput(BaseModel):
    exercise_id: int
    sets: list[SetInput] = Field(default_factory=list)
class CompletionInput(BaseModel):
    user_id: int
    exercises: list[ExercisePerformanceInput] = Field(default_factory=list)
    perceived_exertion: int = Field(ge=1, le=10)
    fatigue: int = Field(ge=0, le=10)
    pain: int = Field(ge=0, le=10)
    duration_minutes: int = Field(ge=1, le=300)
    notes: str = Field("", max_length=2000)
LOG_STATUSES = {"completed", "complete", "done", "partial", "skipped", "planned", "in_progress"}
class WorkoutLogRequest(BaseModel):
    user_id: int
    workout_type: str = Field(min_length=1, max_length=100)
    exercises: list = Field(default_factory=list, max_length=50)
    status: str = Field(min_length=1, max_length=30)
    notes: str = Field("", max_length=2000)
    @field_validator("status")
    @classmethod
    def known_status(cls, value):
        if value.strip().lower() not in LOG_STATUSES: raise ValueError(f"status must be one of {sorted(LOG_STATUSES)}")
        return value.strip().lower()
    @field_validator("exercises")
    @classmethod
    def small_entries(cls, value):
        if any(len(json.dumps(x, ensure_ascii=False)) > 500 for x in value): raise ValueError("exercise entry too large")
        return value
class ReadinessInput(BaseModel):
    energy: int = Field(ge=1, le=10)
    sleep_quality: int = Field(ge=1, le=10)
    soreness: int = Field(ge=0, le=10)
    pain: int = Field(ge=0, le=10)
    notes: str = Field("", max_length=500)


class SubstitutionInput(BaseModel):
    user_id: int
    reason: str = Field("", max_length=300)


class BodyMetricInput(BaseModel):
    weight_kg: Optional[float] = Field(None, ge=30, le=350)
    waist_cm: Optional[float] = Field(None, ge=40, le=250)
    chest_cm: Optional[float] = Field(None, ge=40, le=250)
    arm_cm: Optional[float] = Field(None, ge=10, le=100)
    notes: str = Field("", max_length=500)
    measured_at: Optional[date] = None

    @field_validator("measured_at")
    @classmethod
    def measured_not_future(cls, value):
        if value and value > date.today():
            raise ValueError("measured_at cannot be in the future")
        return value


class HabitInput(BaseModel):
    user_id: int
    name: str = Field(min_length=1, max_length=100)
    anchor: str = Field(min_length=1, max_length=200)
    minimum_routine: str = Field(min_length=1, max_length=300)
    schedule: list[str] = Field(default_factory=list)
    active: bool = True
    _days = field_validator("schedule")(ProfileUpdate.valid_days.__func__)
class HabitComplete(BaseModel):
    user_id: int
    completed_date: date = Field(default_factory=date.today)
    @field_validator("completed_date")
    @classmethod
    def not_in_future(cls, value):
        # A future date combined with the no-backdating rule would permanently
        # lock the habit out of further completions.
        if value > date.today(): raise ValueError("completed_date cannot be in the future")
        return value


async def _user(db, user_id):
    user = await db.get(User, user_id)
    if not user: raise HTTPException(404, "User not found")
    return user

def _profile_dict(p):
    return {"user_id": p.user_id, "equipment": _loads(p.equipment), "health_constraints": _loads(p.health_constraints),
            "goals": _loads(p.goal_type, [p.goal_type]), "supplements": _loads(p.supplement_stack),
            "supplement_notice": "Recorded preferences only; supplements are not prescribed by this service.",
            "preferred_duration_minutes": p.preferred_duration_minutes, "training_days": _loads(p.training_days),
            "training_mode": p.training_mode, "age": p.age, "height_cm": p.height_cm,
            "weight_kg": p.weight_kg, "experience_level": p.experience_level}

@router.get("/profile/{user_id}")
async def get_profile(user_id: int, db: AsyncSession = Depends(get_db), role: str = Depends(read_access)):
    await _user(db, user_id)
    p = await db.get(UserFitnessProfile, user_id)
    if not p:
        return {
            "user_id": user_id,
            "equipment": DEFAULT_EQUIPMENT,
            "health_constraints": DEFAULT_HEALTH_CONSTRAINTS,
            "goals": [DEFAULT_GOAL_TYPE],
            "supplements": DEFAULT_SUPPLEMENT_STACK,
            "supplement_notice": "Recorded preferences only; supplements are not prescribed by this service.",
            "preferred_duration_minutes": 30,
            "training_days": [],
            "training_mode": "family_fitness",
            "age": None,
            "height_cm": None,
            "weight_kg": None,
            "experience_level": "beginner",
            "profile_exists": False,
        }
    return {**_profile_dict(p), "profile_exists": True}

@router.put("/profile/{user_id}")
@router.patch("/profile/{user_id}")
async def update_profile(user_id: int, data: ProfileUpdate, db: AsyncSession = Depends(get_db), role: str = Depends(write_access)):
    await _user(db, user_id)
    p = await db.get(UserFitnessProfile, user_id)
    # health_constraints drive the low-impact safety filter, so a child may not
    # change them. Submitting the unchanged current values is still allowed so
    # the kids UI can edit equipment/goals without a parent present.
    if role == Role.CHILD:
        current = sorted(_loads(p.health_constraints) if p else DEFAULT_HEALTH_CONSTRAINTS)
        if sorted(data.health_constraints) != current:
            raise HTTPException(403, "Only a parent can change health constraints")
    values = dict(equipment=json.dumps(data.equipment), health_constraints=json.dumps(data.health_constraints),
                  goal_type=json.dumps(data.goals), supplement_stack=json.dumps(data.supplements),
                  preferred_duration_minutes=data.preferred_duration_minutes, training_days=json.dumps(data.training_days),
                  training_mode=data.training_mode, age=data.age, height_cm=data.height_cm,
                  weight_kg=data.weight_kg, experience_level=data.experience_level)
    if p:
        for key, value in values.items(): setattr(p, key, value)
    else: p = UserFitnessProfile(user_id=user_id, **values); db.add(p)
    for category, key, value in (("goal", "goals", data.goals), ("safety_constraint", "constraints", data.health_constraints)):
        await memory_manager.add_fact(user_id, user_id, "fitness", category, key, value,
                                      evidence=[{"source": "profile_edit"}], source_agent="fitness", db=db)
    # Store structured preferences in the unified memory layer for cross-agent context.
    await user_memory.store_insights(user_id, "coach", [
        {"category": MemoryCategory.PREFERENCE, "key": "preferred_duration_minutes",
         "value": data.preferred_duration_minutes, "confidence": 1.0,
         "evidence": [{"source": "profile_edit"}]},
        {"category": MemoryCategory.PREFERENCE, "key": "training_days",
         "value": data.training_days, "confidence": 1.0,
         "evidence": [{"source": "profile_edit"}]},
        {"category": MemoryCategory.PREFERENCE, "key": "equipment",
         "value": data.equipment, "confidence": 1.0,
         "evidence": [{"source": "profile_edit"}]},
    ], db=db)
    await db.commit(); await db.refresh(p)
    return _profile_dict(p)

def _readiness_dict(row):
    reduce = row.energy <= 4 or row.sleep_quality <= 4 or row.soreness >= 7 or row.pain > 0
    return {"id": row.id, "user_id": row.user_id, "energy": row.energy, "sleep_quality": row.sleep_quality,
            "soreness": row.soreness, "pain": row.pain, "notes": row.notes or "", "created_at": row.created_at,
            "load_adjustment": "reduce" if reduce else "normal"}


@router.post("/readiness/{user_id}")
async def record_readiness(user_id: int, data: ReadinessInput, db: AsyncSession = Depends(get_db), role: str = Depends(write_access)):
    await _user(db, user_id)
    row = WorkoutReadiness(user_id=user_id, **data.model_dump())
    db.add(row); await db.commit(); await db.refresh(row)
    return _readiness_dict(row)


@router.get("/readiness/{user_id}")
async def latest_readiness(user_id: int, db: AsyncSession = Depends(get_db), role: str = Depends(read_access)):
    await _user(db, user_id)
    row = (await db.execute(select(WorkoutReadiness).where(WorkoutReadiness.user_id == user_id)
                            .order_by(WorkoutReadiness.created_at.desc()).limit(1))).scalar_one_or_none()
    return _readiness_dict(row) if row else {"user_id": user_id, "load_adjustment": "unknown"}


def _body_metric_dict(row):
    return {"id": row.id, "user_id": row.user_id, "weight_kg": row.weight_kg,
            "waist_cm": row.waist_cm, "chest_cm": row.chest_cm, "arm_cm": row.arm_cm,
            "notes": row.notes or "", "measured_at": row.measured_at, "created_at": row.created_at}


@router.post("/body-metrics/{user_id}")
async def record_body_metric(user_id: int, data: BodyMetricInput, db: AsyncSession = Depends(get_db), role: str = Depends(write_access)):
    await _user(db, user_id)
    row = BodyMetric(user_id=user_id, measured_at=data.measured_at or date.today(),
                     weight_kg=data.weight_kg, waist_cm=data.waist_cm,
                     chest_cm=data.chest_cm, arm_cm=data.arm_cm, notes=data.notes)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _body_metric_dict(row)


@router.get("/body-metrics/{user_id}")
async def list_body_metrics(user_id: int, db: AsyncSession = Depends(get_db), role: str = Depends(read_access)):
    await _user(db, user_id)
    rows = (await db.execute(select(BodyMetric).where(BodyMetric.user_id == user_id)
                             .order_by(BodyMetric.measured_at.desc()).limit(90))).scalars().all()
    return [_body_metric_dict(row) for row in rows]


@router.get("/exercises")
async def exercises(category: Optional[str] = None, equipment: Optional[str] = None, low_impact: Optional[bool] = None,
                    muscle_group: Optional[str] = None, limit: int = 200, offset: int = 0,
                    db: AsyncSession = Depends(get_db), role: str = Depends(read_access)):
    q = select(Exercise)
    if category: q = q.where(Exercise.category == category)
    if muscle_group: q = q.where(Exercise.muscle_group == muscle_group)
    if equipment: q = q.where(Exercise.equipment == equipment)
    if low_impact is not None: q = q.where(Exercise.low_impact == int(low_impact))
    rows = (await db.execute(q.order_by(Exercise.name).limit(max(1, min(500, limit))).offset(max(0, offset)))).scalars().all()
    return [{"id": x.id, "slug": x.slug, "name": x.name, "category": x.category,
             "muscle_group": x.muscle_group, "equipment": x.equipment,
             "goals": _loads(x.goals_json), "instructions": x.instructions, "video_url": _normalize_video_url(x.video_url),
             "low_impact": bool(x.low_impact), "high_impact": bool(x.high_impact)} for x in rows]

async def _fetch_directives(db: AsyncSession, user_id: int):
    """Query active parent directives for fitness within the given session."""
    from database import ParentDirective
    from sqlalchemy import or_
    q = select(ParentDirective).where(
        ParentDirective.status == "active",
        or_(ParentDirective.child_id == user_id, ParentDirective.child_id.is_(None)),
        or_(ParentDirective.subject == "fitness", ParentDirective.subject.is_(None)),
    ).order_by(ParentDirective.priority.desc(), ParentDirective.created_at.desc()).limit(20)
    return (await db.execute(q)).scalars().all()

@router.post("/plans/generate/{user_id}")
async def generate_plan(user_id: int, regenerate: bool = False, db: AsyncSession = Depends(get_db), role: str = Depends(write_access)):
    await _user(db, user_id)
    # Idempotency: an untouched plan is reused instead of stacking up duplicates
    # that would otherwise drag the child's adherence rate down.
    if not regenerate:
        open_plan = (await db.execute(
            select(WorkoutPlan).where(WorkoutPlan.user_id == user_id, WorkoutPlan.status == "planned",
                                      WorkoutPlan.planned_date >= date.today())
            .order_by(WorkoutPlan.planned_date, WorkoutPlan.created_at).limit(1)
        )).scalar_one_or_none()
        if open_plan:
            response = await _plan_response(db, open_plan)
            response["coach_greeting"] = _coach_greeting(user_id, response["exercises"])
            response["reused_existing_plan"] = True
            return response
    p = await db.get(UserFitnessProfile, user_id)
    if not p:
        p = UserFitnessProfile(
            user_id=user_id,
            equipment=json.dumps(DEFAULT_EQUIPMENT),
            health_constraints=json.dumps(DEFAULT_HEALTH_CONSTRAINTS),
            goal_type=json.dumps([DEFAULT_GOAL_TYPE]),
            supplement_stack=json.dumps(DEFAULT_SUPPLEMENT_STACK),
            preferred_duration_minutes=30,
            training_days="[]",
        )
        db.add(p)
        await db.flush()
    equipment, constraints, goals = set(_loads(p.equipment)) | {"none"}, _loads(p.health_constraints), _loads(p.goal_type)
    adult_mode = p.training_mode == "adult_hypertrophy"
    if adult_mode:
        goals = set(goals) | {"hypertrophy", "strength"}
    readiness = (await db.execute(select(WorkoutReadiness).where(
        WorkoutReadiness.user_id == user_id,
        WorkoutReadiness.created_at >= datetime.utcnow() - timedelta(hours=24),
    ).order_by(WorkoutReadiness.created_at.desc()).limit(1))).scalar_one_or_none()
    reduce_load = bool(readiness and (readiness.energy <= 4 or readiness.sleep_quality <= 4 or readiness.soreness >= 7 or readiness.pain > 0))
    recent_sessions = (await db.execute(select(WorkoutPerformance).where(
        WorkoutPerformance.user_id == user_id
    ).order_by(WorkoutPerformance.completed_at.desc()).limit(4))).scalars().all()
    fatigue_flags = sum(1 for session in recent_sessions
                        if session.pain > 0 or session.perceived_exertion >= 8 or session.fatigue >= 8)
    recovery_mode = reduce_load or (adult_mode and fatigue_flags >= 2)
    sensitive = any("tilt" in x or "orthostatic" in x for x in constraints) or bool(readiness and readiness.pain > 0)
    # Incorporate parent directives for safety-aware plan generation
    directives = await _fetch_directives(db, user_id)
    directive_texts = [d.message.lower() for d in directives]
    # If any directive requests low-impact or restricts activity, force sensitive mode
    if any(kw in dt for dt in directive_texts for kw in ("low impact", "low-impact", "gentle", "easy", "rest", "no jumping", "avoid high")):
        sensitive = True
    q = select(Exercise).where(Exercise.equipment.in_(equipment))
    if sensitive: q = q.where(Exercise.low_impact == 1, Exercise.high_impact == 0, Exercise.transition != "floor")
    candidates = (await db.execute(q.order_by(Exercise.slug))).scalars().all()

    # Personalize: avoid exercises that recently caused pain or very high exertion.
    avoid_ids = await _exercises_to_avoid(db, user_id)
    candidates = [x for x in candidates if x.id not in avoid_ids]

    # Personalize: boost exercises that were recently completed comfortably
    # and deprioritize the same exercises assigned in the last week.
    comfortable_ids = await _recent_comfortable_exercises(db, user_id)
    recently_used_ids = await _recently_used_exercises(db, user_id)

    def sort_key(x):
        goal_overlap = len(set(_loads(x.goals_json)) & set(goals))
        comfort_bonus = 1 if x.id in comfortable_ids else 0
        recent_penalty = 1 if x.id in recently_used_ids else 0
        return (-goal_overlap, -comfort_bonus, recent_penalty, _muscle_group_sort(x), random.random())

    candidates.sort(key=sort_key)
    if adult_mode:
        exercise_count = max(3, min(6, p.preferred_duration_minutes // 10))
        chosen, used_groups = [], set()
        for exercise in candidates:
            if exercise.muscle_group not in used_groups:
                chosen.append(exercise)
                used_groups.add(exercise.muscle_group)
            if len(chosen) == exercise_count:
                break
        chosen.extend(exercise for exercise in candidates
                      if exercise not in chosen and len(chosen) < exercise_count)
    else:
        chosen = candidates[:max(1, min(5, p.preferred_duration_minutes // 6))]
    if not chosen: raise HTTPException(422, "No safe exercises match this profile")
    notes = (["Low-impact exercises only.", "Use slow, controlled transitions and avoid sudden floor-to-standing changes.", "Stop if dizzy, faint, or in pain and seek qualified clinical advice when appropriate."] if sensitive else ["Use controlled form and stop if pain or concerning symptoms occur."])
    adaptations = [{"type": "training_mode", "value": p.training_mode}] if adult_mode else []
    if adult_mode:
        adaptations.append({"type": "experience", "value": p.experience_level,
                            "effect": "sets, rep target, rest, and RIR calibrated to experience"})
    if reduce_load:
        notes.append("Readiness check indicates reduced volume today; keep effort controlled and leave several reps in reserve.")
        adaptations.append({"type": "readiness", "value": "reduce_volume",
                            "reason": "low energy, poor sleep, soreness, or pain reported in the last 24 hours"})
    if recovery_mode and not reduce_load:
        adaptations.append({"type": "fatigue_trend", "value": "deload",
                            "reason": "multiple recent sessions reported high exertion, fatigue, or pain"})
    if sensitive:
        adaptations.append({"type": "safety", "value": "low_impact_only"})
    # Append directive notes to safety notes
    for d in directives:
        notes.append(f"Parent directive: {d.message}")
    # Append cross-agent memory notes (e.g., recent pain, fatigue, preferences).
    memory_notes = await _memory_notes_for_plan(user_id)
    notes.extend(memory_notes)
    # Apply the progression earned in previous sessions, one variable at a time.
    targets = await _progression_targets(db, user_id, [x.id for x in chosen])
    progressed_targets = [t for t in targets.values() if t["progressed"]]
    if progressed_targets:
        notes.append("One small increase applied where your last session felt comfortable.")
        adaptations.append({"type": "progression", "value": "one_variable_increase",
                            "exercise_count": len(progressed_targets)})
    plan = WorkoutPlan(user_id=user_id, planned_date=_next_training_date(_loads(p.training_days)),
                       duration_minutes=p.preferred_duration_minutes, safety_notes_json=json.dumps(notes),
                       adaptations_json=json.dumps(adaptations, ensure_ascii=False)); db.add(plan); await db.flush()
    prescription = _adult_prescription(p.experience_level)
    for i, exercise in enumerate(chosen):
        target = targets.get(exercise.id, {"sets": prescription["sets"] if adult_mode else BASELINE_SETS,
                                         "reps": prescription["reps"] if adult_mode else BASELINE_REPS})
        target_rir = prescription["target_rir"] if adult_mode else None
        if recovery_mode:
            target_sets = min(target["sets"], 2)
            if target_rir is not None:
                target_rir = max(target_rir, 3.0)
        else:
            target_sets = target["sets"]
        target_resistance = target.get("resistance")
        if recovery_mode and target_resistance is not None:
            target_resistance = round(target_resistance * 0.95, 2)
        db.add(PlannedWorkoutExercise(plan_id=plan.id, exercise_id=exercise.id, position=i, sets=target_sets,
                                      reps=target["reps"], target_resistance=target_resistance,
                                      target_rir=target_rir,
                                      rest_seconds=prescription["rest_seconds"] if adult_mode else 60,
                                      minimum_version="1 controlled set of 5 reps"))
    await db.commit()
    await db.refresh(plan)
    response = await _plan_response(db, plan)
    response["coach_greeting"] = _coach_greeting(user_id, response["exercises"])
    return response

async def _plan_response(db, plan):
    rows = (await db.execute(select(PlannedWorkoutExercise, Exercise).join(Exercise).where(PlannedWorkoutExercise.plan_id == plan.id).order_by(PlannedWorkoutExercise.position))).all()
    return {"id": plan.id, "user_id": plan.user_id, "date": plan.planned_date, "status": plan.status, "duration_minutes": plan.duration_minutes,
            "safety_notes": _loads(plan.safety_notes_json), "adaptations": _loads(plan.adaptations_json),
            "exercises": [{"exercise_id": e.id, "name": e.name, "muscle_group": e.muscle_group,
                           "video_url": _normalize_video_url(e.video_url), "sets": pe.sets, "reps": pe.reps,
                           "target_resistance": pe.target_resistance, "target_rir": pe.target_rir,
                           "duration_seconds": pe.duration_seconds, "rest_seconds": pe.rest_seconds,
                           "minimum_version": pe.minimum_version} for pe,e in rows]}

@router.get("/plans/{user_id}")
async def list_plans(user_id: int, db: AsyncSession = Depends(get_db), role: str = Depends(read_access)):
    await _user(db,user_id); rows=(await db.execute(select(WorkoutPlan).where(WorkoutPlan.user_id==user_id).order_by(WorkoutPlan.created_at.desc()).limit(20))).scalars().all()
    return [await _plan_response(db,p) for p in rows]

@router.post("/plans/{plan_id}/substitute/{exercise_id}")
async def substitute_exercise(plan_id: int, exercise_id: int, data: SubstitutionInput,
                              db: AsyncSession = Depends(get_db), role: str = Depends(write_access)):
    await _user(db, data.user_id)
    plan = await db.get(WorkoutPlan, plan_id)
    if not plan or plan.user_id != data.user_id:
        raise HTTPException(404, "Plan not found")
    if plan.status == "completed":
        raise HTTPException(409, "Cannot substitute exercises in a completed plan")
    planned = (await db.execute(select(PlannedWorkoutExercise, Exercise)
                                .join(Exercise, Exercise.id == PlannedWorkoutExercise.exercise_id)
                                .where(PlannedWorkoutExercise.plan_id == plan_id,
                                       PlannedWorkoutExercise.exercise_id == exercise_id))).first()
    if not planned:
        raise HTTPException(404, "Exercise is not in this plan")
    planned_row, current = planned
    profile = await db.get(UserFitnessProfile, data.user_id)
    equipment = set(_loads(profile.equipment)) | {"none"} if profile else set(DEFAULT_EQUIPMENT)
    constraints = _loads(profile.health_constraints) if profile else DEFAULT_HEALTH_CONSTRAINTS
    sensitive = any("tilt" in x or "orthostatic" in x or "low_impact" in x for x in constraints)
    plan_ids = set((await db.execute(select(PlannedWorkoutExercise.exercise_id)
                                     .where(PlannedWorkoutExercise.plan_id == plan_id))).scalars())
    avoid_ids = await _exercises_to_avoid(db, data.user_id)
    q = select(Exercise).where(
        Exercise.muscle_group == current.muscle_group,
        Exercise.equipment.in_(equipment),
        ~Exercise.id.in_(plan_ids | avoid_ids),
    )
    if sensitive:
        q = q.where(Exercise.low_impact == 1, Exercise.high_impact == 0, Exercise.transition != "floor")
    replacement = (await db.execute(q.order_by(Exercise.slug).limit(1))).scalar_one_or_none()
    if not replacement:
        raise HTTPException(422, "No safe substitute is available for this exercise")
    current_id, current_group = current.id, current.muscle_group
    replacement_id, replacement_group = replacement.id, replacement.muscle_group
    planned_row.exercise_id = replacement_id
    await db.commit()
    await db.refresh(planned_row)
    await db.refresh(plan)
    response = await _plan_response(db, plan)
    response["substitution"] = {
        "from_exercise_id": current_id,
        "to_exercise_id": replacement_id,
        "reason": data.reason,
        "muscle_group": replacement_group,
    }
    return response


@router.post("/plans/{plan_id}/complete")
async def complete_plan(plan_id: int, data: CompletionInput, db: AsyncSession = Depends(get_db), role: str = Depends(write_access)):
    await _user(db,data.user_id); plan=await db.get(WorkoutPlan,plan_id)
    if not plan or plan.user_id != data.user_id: raise HTTPException(404,"Plan not found")
    if plan.status == "completed": raise HTTPException(409,"Plan already completed")
    valid=set((await db.execute(select(PlannedWorkoutExercise.exercise_id).where(PlannedWorkoutExercise.plan_id==plan_id))).scalars())
    if any(x.exercise_id not in valid for x in data.exercises): raise HTTPException(422,"Exercise is not in this plan")
    # Claim the plan with a conditional UPDATE so two concurrent submissions
    # cannot both record a performance for the same session.
    claimed = await db.execute(update(WorkoutPlan).where(WorkoutPlan.id == plan_id, WorkoutPlan.status != "completed")
                               .values(status="completed", completed_at=datetime.utcnow()))
    if claimed.rowcount == 0: raise HTTPException(409,"Plan already completed")
    blocked=data.pain>0 or data.perceived_exertion>=9 or data.fatigue>=8
    guidance=("Do not progress. Stop activity if pain or concerning symptoms continue and seek advice from a qualified clinician when appropriate; this is not a diagnosis." if blocked else "Continue at the same level; after another comfortable session, consider one small increase in reps or resistance.")
    perf=WorkoutPerformance(plan_id=plan_id,user_id=data.user_id,perceived_exertion=data.perceived_exertion,fatigue=data.fatigue,pain=data.pain,duration_minutes=data.duration_minutes,notes=data.notes,guidance=guidance,progression_allowed=int(not blocked)); db.add(perf); await db.flush()
    details=[]
    for item in data.exercises:
        for s in item.sets: db.add(WorkoutSet(performance_id=perf.id,exercise_id=item.exercise_id,**s.model_dump()))
        details.append({"exercise_id":item.exercise_id,"sets":[s.model_dump() for s in item.sets]})
        if not blocked: db.add(ProgressionHistory(user_id=data.user_id,exercise_id=item.exercise_id,performance_id=perf.id,recommendation="Maintain, then increase only one variable slightly after another comfortable session.",evidence_json=json.dumps({"rpe":data.perceived_exertion,"fatigue":data.fatigue,"pain":data.pain})))
    performance_id = perf.id
    await memory_manager.add_fact(data.user_id,data.user_id,"fitness","recommendation","latest_progress",guidance,evidence=[{"plan_id":plan_id,"performance_id":performance_id}],source_agent="fitness",db=db)

    # Store cross-agent memory insights from this workout.
    insights = _workout_insights(data, plan_id, blocked)
    await user_memory.add_episode(data.user_id, "coach", "workout_completed", {
        "plan_id": plan_id,
        "duration_minutes": data.duration_minutes,
        "rpe": data.perceived_exertion,
        "fatigue": data.fatigue,
        "pain": data.pain,
        "blocked": blocked,
    }, db=db)
    if insights:
        await user_memory.store_insights(data.user_id, "coach", insights, db=db)

    await db.commit()
    # NOTE: every value used below is a plain Python local captured before the
    # commit. Reading an ORM attribute here would expire-and-reload the row and
    # raise MissingGreenlet inside the async request, so keep it that way.
    payload={"plan_id":plan_id,"performance_id":performance_id,"workout_type":"planned","duration_minutes":data.duration_minutes,"perceived_exertion":data.perceived_exertion,"fatigue":data.fatigue,"pain":data.pain,"progression_allowed":not blocked,"exercises":details}
    # The workout is already durably stored; a collaboration failure must not
    # fail the child's request or lose their session.
    try:
        await event_bus.publish("fitness.workout_completed",user_id=data.user_id,child_id=data.user_id,source_agent="fitness",payload=payload)
    except Exception:
        logger.exception("Failed to publish fitness.workout_completed", plan_id=plan_id)
    return {"performance_id":performance_id,"progression_allowed":not blocked,"guidance":guidance}

@router.post("/log")
async def log_workout(data: WorkoutLogRequest, db: AsyncSession = Depends(get_db), role: str = Depends(write_access)):
    await _user(db,data.user_id)
    exercises_json = json.dumps(data.exercises, ensure_ascii=False)
    row=HealthLog(user_id=data.user_id,workout_type=data.workout_type,exercises_json=exercises_json,status=data.status,notes=data.notes,log_type="workout",details_json=exercises_json); db.add(row); await db.flush()
    await user_memory.add_episode(
        data.user_id, "coach", "logged_workout",
        {"workout_type": data.workout_type, "status": data.status, "exercises_count": len(data.exercises)},
        metadata={"health_log_id": row.id, "notes": data.notes[:200]},
        db=db,
    )
    await db.commit(); await db.refresh(row)
    health_log_id = row.id
    if data.status in {"completed","complete","done"}:
        try:
            await event_bus.publish("fitness.workout_completed",data.user_id,data.user_id,"fitness",{"health_log_id":health_log_id,"workout_type":data.workout_type,"exercises":data.exercises})
        except Exception:
            logger.exception("Failed to publish fitness.workout_completed", health_log_id=health_log_id)
    return {"status":"success","health_log_id":health_log_id,"message":"Workout logged and synced with cross-agent state."}

@router.post("/habits")
async def create_habit(data: HabitInput, db: AsyncSession=Depends(get_db), role: str = Depends(write_access)):
    await _user(db,data.user_id); h=TinyHabit(user_id=data.user_id,name=data.name,anchor=data.anchor,minimum_routine=data.minimum_routine,schedule_json=json.dumps(data.schedule),active=int(data.active)); db.add(h); await db.commit(); await db.refresh(h); return _habit(h)
@router.put("/habits/{habit_id}")
async def update_habit(habit_id:int,data:HabitInput,db:AsyncSession=Depends(get_db), role: str = Depends(write_access)):
    await _user(db,data.user_id)
    h=await db.get(TinyHabit,habit_id)
    if not h or h.user_id!=data.user_id: raise HTTPException(404,"Habit not found")
    for k,v in {"name":data.name,"anchor":data.anchor,"minimum_routine":data.minimum_routine,"schedule_json":json.dumps(data.schedule),"active":int(data.active)}.items(): setattr(h,k,v)
    await db.commit(); await db.refresh(h); return _habit(h)
def _habit(h): return {"id":h.id,"user_id":h.user_id,"name":h.name,"anchor":h.anchor,"minimum_routine":h.minimum_routine,"schedule":_loads(h.schedule_json),"current_streak":h.current_streak,"best_streak":h.best_streak,"last_completion":h.last_completion,"active":bool(h.active)}
@router.get("/habits/{user_id}")
async def habits(user_id:int,db:AsyncSession=Depends(get_db), role: str = Depends(read_access)):
    await _user(db,user_id); return [_habit(h) for h in (await db.execute(select(TinyHabit).where(TinyHabit.user_id==user_id))).scalars().all()]
@router.post("/habits/{habit_id}/complete")
async def habit_complete(habit_id:int,data:HabitComplete,db:AsyncSession=Depends(get_db), role: str = Depends(write_access)):
    h=await db.get(TinyHabit,habit_id)
    if not h or h.user_id!=data.user_id: raise HTTPException(404,"Habit not found")
    if h.last_completion and data.completed_date < h.last_completion: raise HTTPException(409,"Cannot backdate before latest completion")
    if h.last_completion != data.completed_date:
        h.current_streak = h.current_streak+1 if h.last_completion==data.completed_date-timedelta(days=1) else 1
        h.best_streak=max(h.best_streak,h.current_streak); h.last_completion=data.completed_date
        db.add(TinyHabitCompletion(habit_id=habit_id, user_id=data.user_id, completed_date=data.completed_date))
        await user_memory.store_insights(data.user_id, "coach", [{
            "category": MemoryCategory.BEHAVIOR,
            "key": f"habit_{h.id}_streak",
            "value": f"'{h.name}' habit current streak is {h.current_streak} and best streak is {h.best_streak}.",
            "confidence": 1.0,
        }], db=db)
        await db.commit()
        await db.refresh(h)
    return _habit(h)
@router.get("/habits/{habit_id}/history")
async def habit_history(habit_id:int, user_id:int, db:AsyncSession=Depends(get_db), role: str = Depends(read_access)):
    await _user(db, user_id)
    h=await db.get(TinyHabit,habit_id)
    if not h or h.user_id != user_id: raise HTTPException(404,"Habit not found")
    rows=(await db.execute(select(TinyHabitCompletion).where(TinyHabitCompletion.habit_id==habit_id).order_by(TinyHabitCompletion.completed_date.desc()).limit(90))).scalars().all()
    return {"habit_id":habit_id,"completions":[{"id":r.id,"date":str(r.completed_date),"created_at":str(r.created_at)} for r in rows]}
@router.get("/habits/{user_id}/minimum-routine")
async def minimum_routine(user_id:int,db:AsyncSession=Depends(get_db), role: str = Depends(read_access)):
    await _user(db,user_id); rows=(await db.execute(select(TinyHabit).where(TinyHabit.user_id==user_id,TinyHabit.active==1))).scalars().all(); return {"routine":[{"habit_id":h.id,"after":h.anchor,"do":h.minimum_routine} for h in rows]}

@router.get("/analytics/{user_id}")
async def analytics(user_id:int, window_days:int=30, db:AsyncSession=Depends(get_db), role: str = Depends(read_access)):
    await _user(db,user_id)
    window_days=max(1,min(365,window_days)); since=date.today()-timedelta(days=window_days)
    plans=(await db.execute(select(WorkoutPlan).where(WorkoutPlan.user_id==user_id,WorkoutPlan.planned_date>=since))).scalars().all()
    recent=(await db.execute(select(WorkoutPerformance).where(WorkoutPerformance.user_id==user_id).order_by(WorkoutPerformance.completed_at.desc()).limit(10))).scalars().all()
    progress=(await db.execute(select(ProgressionHistory).where(ProgressionHistory.user_id==user_id).order_by(ProgressionHistory.created_at.desc()).limit(20))).scalars().all()
    hs=(await db.execute(select(TinyHabit).where(TinyHabit.user_id==user_id))).scalars().all()
    # Adherence is measured only over plans whose day has passed. A plan still
    # scheduled for today or later has not been missed yet, so counting it
    # would punish the child for the Coach having offered them a session.
    resolved=[p for p in plans if p.status=="completed" or p.planned_date<date.today()]
    completed=sum(p.status=="completed" for p in resolved)
    week_cutoff = datetime.utcnow() - timedelta(days=7)
    strength_rows = (await db.execute(
        select(WorkoutSet, Exercise.muscle_group)
        .join(WorkoutPerformance, WorkoutPerformance.id == WorkoutSet.performance_id)
        .outerjoin(Exercise, Exercise.id == WorkoutSet.exercise_id)
        .where(WorkoutPerformance.user_id == user_id, WorkoutPerformance.completed_at >= week_cutoff)
    )).all()
    strength_sets = [row[0] for row in strength_rows]
    resistance_sets = [s for s in strength_sets if s.resistance is not None and s.reps is not None]
    rir_values = [float(s.rir) for s in strength_sets if s.rir is not None]
    volume_load = sum(float(s.resistance) * int(s.reps) for s in resistance_sets)
    sets_by_muscle = {}
    for workout_set, muscle_group in strength_rows:
        group = muscle_group or "full_body"
        sets_by_muscle.setdefault(group, {"sets": 0, "volume_load": 0.0})
        sets_by_muscle[group]["sets"] += 1
        if workout_set.resistance is not None and workout_set.reps is not None:
            sets_by_muscle[group]["volume_load"] += float(workout_set.resistance) * int(workout_set.reps)
    profile = await db.get(UserFitnessProfile, user_id)
    experience = profile.experience_level if profile else "beginner"
    weekly_target = MUSCLE_VOLUME_TARGETS.get(experience, MUSCLE_VOLUME_TARGETS["beginner"])["weekly_sets_per_muscle"]
    readiness_rows = (await db.execute(select(WorkoutReadiness).where(
        WorkoutReadiness.user_id == user_id,
        WorkoutReadiness.created_at >= datetime.utcnow() - timedelta(days=14),
    ).order_by(WorkoutReadiness.created_at.desc()))).scalars().all()
    metrics = (await db.execute(select(BodyMetric).where(BodyMetric.user_id == user_id)
                                .order_by(BodyMetric.measured_at.desc()).limit(10))).scalars().all()
    latest_metric = _body_metric_dict(metrics[0]) if metrics else None
    weight_change = None
    if len(metrics) >= 2 and metrics[0].weight_kg is not None and metrics[-1].weight_kg is not None:
        weight_change = round(metrics[0].weight_kg - metrics[-1].weight_kg, 2)
    return {"adherence":{"planned":len(resolved),"completed":completed,"missed":len(resolved)-completed,
                         "upcoming":len(plans)-len(resolved),"window_days":window_days,
                         "rate":round(completed/len(resolved),3) if resolved else 0},
            "recent_sessions":[{"id":r.id,"completed_at":r.completed_at,"duration_minutes":r.duration_minutes,"rpe":r.perceived_exertion,"fatigue":r.fatigue,"pain":r.pain,"progression_allowed":bool(r.progression_allowed)} for r in recent],
            "progression":[{"exercise_id":p.exercise_id,"recommendation":p.recommendation,"created_at":p.created_at} for p in progress],
            "streaks":[{"habit_id":h.id,"name":h.name,"current":h.current_streak,"best":h.best_streak} for h in hs],
            "strength_training":{"weekly_working_sets":len(strength_sets),
                                 "total_volume_load":round(volume_load,2),
                                 "average_rir":round(sum(rir_values)/len(rir_values),2) if rir_values else None,
                                 "sets_by_muscle_group":{k:{"sets":v["sets"],"volume_load":round(v["volume_load"],2)} for k,v in sets_by_muscle.items()},
                                 "weekly_sets_per_muscle_target":weekly_target},
            "recovery_trend":{"checkins":len(readiness_rows),
                              "average_energy":round(sum(r.energy for r in readiness_rows)/len(readiness_rows),2) if readiness_rows else None,
                              "average_sleep":round(sum(r.sleep_quality for r in readiness_rows)/len(readiness_rows),2) if readiness_rows else None,
                              "average_soreness":round(sum(r.soreness for r in readiness_rows)/len(readiness_rows),2) if readiness_rows else None},
            "body_metrics":{"latest":latest_metric,"weight_change_kg":weight_change},
            "goal_status":{"completed_workouts":completed,"status":"in_progress" if plans else "not_started"}}
