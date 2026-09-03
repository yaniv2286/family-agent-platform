import json
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import (Exercise, HealthLog, PlannedWorkoutExercise, ProgressionHistory,
                      TinyHabit, TinyHabitCompletion, User, UserFitnessProfile,
                      WorkoutPerformance, WorkoutPlan, WorkoutSet, get_db)
from event_bus import event_bus
from memory_manager import memory_manager
import nutrition_agent  # register collaboration handler

router = APIRouter(prefix="/api/fitness", tags=["Fitness Agent"])
DEFAULT_EQUIPMENT = ["none", "balance_board", "balance_pad", "fitball", "pull_up_bar", "resistance_bands", "pilates_mat", "trx"]
DEFAULT_HEALTH_CONSTRAINTS = ["tilt_test_sensitivity", "avoid_sudden_orthostatic_drops", "low_impact_strength"]
DEFAULT_GOAL_TYPE = "hypertrophy_body_recomposition_fat_loss"
DEFAULT_SUPPLEMENT_STACK = ["creatine", "whey_protein"]
DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def _loads(value, fallback=None):
    try: return json.loads(value or "[]")
    except (TypeError, ValueError): return fallback or []


class ProfileUpdate(BaseModel):
    equipment: list[str] = Field(default_factory=list)
    health_constraints: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    supplements: list[str] = Field(default_factory=list)
    preferred_duration_minutes: int = Field(30, ge=5, le=120)
    training_days: list[str] = Field(default_factory=list)
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
class WorkoutLogRequest(BaseModel):
    user_id: int
    workout_type: str = Field(min_length=1, max_length=100)
    exercises: list = Field(default_factory=list)
    status: str
    notes: str = Field("", max_length=2000)
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


async def _user(db, user_id):
    user = await db.get(User, user_id)
    if not user: raise HTTPException(404, "User not found")
    return user

def _profile_dict(p):
    return {"user_id": p.user_id, "equipment": _loads(p.equipment), "health_constraints": _loads(p.health_constraints),
            "goals": _loads(p.goal_type, [p.goal_type]), "supplements": _loads(p.supplement_stack),
            "supplement_notice": "Recorded preferences only; supplements are not prescribed by this service.",
            "preferred_duration_minutes": p.preferred_duration_minutes, "training_days": _loads(p.training_days)}

@router.get("/profile/{user_id}")
async def get_profile(user_id: int, db: AsyncSession = Depends(get_db)):
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
            "profile_exists": False,
        }
    return {**_profile_dict(p), "profile_exists": True}

@router.put("/profile/{user_id}")
@router.patch("/profile/{user_id}")
async def update_profile(user_id: int, data: ProfileUpdate, db: AsyncSession = Depends(get_db)):
    await _user(db, user_id)
    p = await db.get(UserFitnessProfile, user_id)
    values = dict(equipment=json.dumps(data.equipment), health_constraints=json.dumps(data.health_constraints),
                  goal_type=json.dumps(data.goals), supplement_stack=json.dumps(data.supplements),
                  preferred_duration_minutes=data.preferred_duration_minutes, training_days=json.dumps(data.training_days))
    if p:
        for key, value in values.items(): setattr(p, key, value)
    else: p = UserFitnessProfile(user_id=user_id, **values); db.add(p)
    for category, key, value in (("goal", "goals", data.goals), ("safety_constraint", "constraints", data.health_constraints)):
        await memory_manager.add_fact(user_id, user_id, "fitness", category, key, value,
                                      evidence=[{"source": "profile_edit"}], source_agent="fitness", db=db)
    await db.commit(); await db.refresh(p)
    return _profile_dict(p)

@router.get("/exercises")
async def exercises(category: Optional[str] = None, equipment: Optional[str] = None, low_impact: Optional[bool] = None, db: AsyncSession = Depends(get_db)):
    q = select(Exercise)
    if category: q = q.where(Exercise.category == category)
    if equipment: q = q.where(Exercise.equipment == equipment)
    if low_impact is not None: q = q.where(Exercise.low_impact == int(low_impact))
    rows = (await db.execute(q.order_by(Exercise.name))).scalars().all()
    return [{"id": x.id, "slug": x.slug, "name": x.name, "category": x.category, "equipment": x.equipment,
             "goals": _loads(x.goals_json), "instructions": x.instructions, "low_impact": bool(x.low_impact), "high_impact": bool(x.high_impact)} for x in rows]

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
async def generate_plan(user_id: int, db: AsyncSession = Depends(get_db)):
    await _user(db, user_id); p = await db.get(UserFitnessProfile, user_id)
    if not p: raise HTTPException(400, "Create a fitness profile first")
    equipment, constraints, goals = set(_loads(p.equipment)) | {"none"}, _loads(p.health_constraints), _loads(p.goal_type)
    sensitive = any("tilt" in x or "orthostatic" in x for x in constraints)
    # Incorporate parent directives for safety-aware plan generation
    directives = await _fetch_directives(db, user_id)
    directive_texts = [d.message.lower() for d in directives]
    # If any directive requests low-impact or restricts activity, force sensitive mode
    if any(kw in dt for dt in directive_texts for kw in ("low impact", "low-impact", "gentle", "easy", "rest", "no jumping", "avoid high")):
        sensitive = True
    q = select(Exercise).where(Exercise.equipment.in_(equipment))
    if sensitive: q = q.where(Exercise.low_impact == 1, Exercise.high_impact == 0, Exercise.transition != "floor")
    candidates = (await db.execute(q.order_by(Exercise.slug))).scalars().all()
    candidates.sort(key=lambda x: (-len(set(_loads(x.goals_json)) & set(goals)), x.slug))
    chosen = candidates[:max(1, min(5, p.preferred_duration_minutes // 6))]
    if not chosen: raise HTTPException(422, "No safe exercises match this profile")
    notes = (["Low-impact exercises only.", "Use slow, controlled transitions and avoid sudden floor-to-standing changes.", "Stop if dizzy, faint, or in pain and seek qualified clinical advice when appropriate."] if sensitive else ["Use controlled form and stop if pain or concerning symptoms occur."])
    # Append directive notes to safety notes
    for d in directives:
        notes.append(f"Parent directive: {d.message}")
    plan = WorkoutPlan(user_id=user_id, planned_date=date.today(), duration_minutes=p.preferred_duration_minutes, safety_notes_json=json.dumps(notes)); db.add(plan); await db.flush()
    for i, exercise in enumerate(chosen):
        db.add(PlannedWorkoutExercise(plan_id=plan.id, exercise_id=exercise.id, position=i, sets=2, reps=8, rest_seconds=60, minimum_version="1 controlled set of 5 reps"))
    await db.commit()
    return await _plan_response(db, plan)

async def _plan_response(db, plan):
    rows = (await db.execute(select(PlannedWorkoutExercise, Exercise).join(Exercise).where(PlannedWorkoutExercise.plan_id == plan.id).order_by(PlannedWorkoutExercise.position))).all()
    return {"id": plan.id, "user_id": plan.user_id, "date": plan.planned_date, "status": plan.status, "duration_minutes": plan.duration_minutes,
            "safety_notes": _loads(plan.safety_notes_json), "exercises": [{"exercise_id": e.id, "name": e.name, "sets": pe.sets, "reps": pe.reps, "duration_seconds": pe.duration_seconds, "rest_seconds": pe.rest_seconds, "minimum_version": pe.minimum_version} for pe,e in rows]}

@router.get("/plans/{user_id}")
async def list_plans(user_id: int, db: AsyncSession = Depends(get_db)):
    await _user(db,user_id); rows=(await db.execute(select(WorkoutPlan).where(WorkoutPlan.user_id==user_id).order_by(WorkoutPlan.created_at.desc()).limit(20))).scalars().all()
    return [await _plan_response(db,p) for p in rows]

@router.post("/plans/{plan_id}/complete")
async def complete_plan(plan_id: int, data: CompletionInput, db: AsyncSession = Depends(get_db)):
    await _user(db,data.user_id); plan=await db.get(WorkoutPlan,plan_id)
    if not plan or plan.user_id != data.user_id: raise HTTPException(404,"Plan not found")
    if plan.status == "completed": raise HTTPException(409,"Plan already completed")
    valid=set((await db.execute(select(PlannedWorkoutExercise.exercise_id).where(PlannedWorkoutExercise.plan_id==plan_id))).scalars())
    if any(x.exercise_id not in valid for x in data.exercises): raise HTTPException(422,"Exercise is not in this plan")
    blocked=data.pain>0 or data.perceived_exertion>=9 or data.fatigue>=8
    guidance=("Do not progress. Stop activity if pain or concerning symptoms continue and seek advice from a qualified clinician when appropriate; this is not a diagnosis." if blocked else "Continue at the same level; after another comfortable session, consider one small increase in reps or resistance.")
    perf=WorkoutPerformance(plan_id=plan_id,user_id=data.user_id,perceived_exertion=data.perceived_exertion,fatigue=data.fatigue,pain=data.pain,duration_minutes=data.duration_minutes,notes=data.notes,guidance=guidance,progression_allowed=int(not blocked)); db.add(perf); await db.flush()
    details=[]
    for item in data.exercises:
        for s in item.sets: db.add(WorkoutSet(performance_id=perf.id,exercise_id=item.exercise_id,**s.model_dump()))
        details.append({"exercise_id":item.exercise_id,"sets":[s.model_dump() for s in item.sets]})
        if not blocked: db.add(ProgressionHistory(user_id=data.user_id,exercise_id=item.exercise_id,performance_id=perf.id,recommendation="Maintain, then increase only one variable slightly after another comfortable session.",evidence_json=json.dumps({"rpe":data.perceived_exertion,"fatigue":data.fatigue,"pain":data.pain})))
    plan.status="completed"; plan.completed_at=datetime.utcnow(); await memory_manager.add_fact(data.user_id,data.user_id,"fitness","recommendation","latest_progress",guidance,evidence=[{"plan_id":plan_id,"performance_id":perf.id}],source_agent="fitness",db=db)
    await db.commit()
    payload={"plan_id":plan_id,"performance_id":perf.id,"workout_type":"planned","duration_minutes":data.duration_minutes,"perceived_exertion":data.perceived_exertion,"fatigue":data.fatigue,"pain":data.pain,"progression_allowed":not blocked,"exercises":details}
    await event_bus.publish("fitness.workout_completed",user_id=data.user_id,child_id=data.user_id,source_agent="fitness",payload=payload)
    return {"performance_id":perf.id,"progression_allowed":not blocked,"guidance":guidance}

@router.post("/log")
async def log_workout(data: WorkoutLogRequest, db: AsyncSession = Depends(get_db)):
    await _user(db,data.user_id)
    exercises_json = json.dumps(data.exercises, ensure_ascii=False)
    row=HealthLog(user_id=data.user_id,workout_type=data.workout_type,exercises_json=exercises_json,status=data.status,notes=data.notes,log_type="workout",details_json=exercises_json); db.add(row); await db.commit(); await db.refresh(row)
    if data.status.lower() in {"completed","complete","done"}: await event_bus.publish("fitness.workout_completed",data.user_id,data.user_id,"fitness",{"health_log_id":row.id,"workout_type":data.workout_type,"exercises":data.exercises})
    return {"status":"success","health_log_id":row.id,"message":"Workout logged and synced with cross-agent state."}

@router.post("/habits")
async def create_habit(data: HabitInput, db: AsyncSession=Depends(get_db)):
    await _user(db,data.user_id); h=TinyHabit(user_id=data.user_id,name=data.name,anchor=data.anchor,minimum_routine=data.minimum_routine,schedule_json=json.dumps(data.schedule),active=int(data.active)); db.add(h); await db.commit(); await db.refresh(h); return _habit(h)
@router.put("/habits/{habit_id}")
async def update_habit(habit_id:int,data:HabitInput,db:AsyncSession=Depends(get_db)):
    h=await db.get(TinyHabit,habit_id)
    if not h or h.user_id!=data.user_id: raise HTTPException(404,"Habit not found")
    for k,v in {"name":data.name,"anchor":data.anchor,"minimum_routine":data.minimum_routine,"schedule_json":json.dumps(data.schedule),"active":int(data.active)}.items(): setattr(h,k,v)
    await db.commit(); return _habit(h)
def _habit(h): return {"id":h.id,"user_id":h.user_id,"name":h.name,"anchor":h.anchor,"minimum_routine":h.minimum_routine,"schedule":_loads(h.schedule_json),"current_streak":h.current_streak,"best_streak":h.best_streak,"last_completion":h.last_completion,"active":bool(h.active)}
@router.get("/habits/{user_id}")
async def habits(user_id:int,db:AsyncSession=Depends(get_db)):
    await _user(db,user_id); return [_habit(h) for h in (await db.execute(select(TinyHabit).where(TinyHabit.user_id==user_id))).scalars().all()]
@router.post("/habits/{habit_id}/complete")
async def habit_complete(habit_id:int,data:HabitComplete,db:AsyncSession=Depends(get_db)):
    h=await db.get(TinyHabit,habit_id)
    if not h or h.user_id!=data.user_id: raise HTTPException(404,"Habit not found")
    if h.last_completion and data.completed_date < h.last_completion: raise HTTPException(409,"Cannot backdate before latest completion")
    if h.last_completion != data.completed_date:
        h.current_streak = h.current_streak+1 if h.last_completion==data.completed_date-timedelta(days=1) else 1
        h.best_streak=max(h.best_streak,h.current_streak); h.last_completion=data.completed_date
        db.add(TinyHabitCompletion(habit_id=habit_id, user_id=data.user_id, completed_date=data.completed_date))
        await db.commit()
    return _habit(h)
@router.get("/habits/{habit_id}/history")
async def habit_history(habit_id:int, user_id:int, db:AsyncSession=Depends(get_db)):
    await _user(db, user_id)
    h=await db.get(TinyHabit,habit_id)
    if not h or h.user_id != user_id: raise HTTPException(404,"Habit not found")
    rows=(await db.execute(select(TinyHabitCompletion).where(TinyHabitCompletion.habit_id==habit_id).order_by(TinyHabitCompletion.completed_date.desc()).limit(90))).scalars().all()
    return {"habit_id":habit_id,"completions":[{"id":r.id,"date":str(r.completed_date),"created_at":str(r.created_at)} for r in rows]}
@router.get("/habits/{user_id}/minimum-routine")
async def minimum_routine(user_id:int,db:AsyncSession=Depends(get_db)):
    await _user(db,user_id); rows=(await db.execute(select(TinyHabit).where(TinyHabit.user_id==user_id,TinyHabit.active==1))).scalars().all(); return {"routine":[{"habit_id":h.id,"after":h.anchor,"do":h.minimum_routine} for h in rows]}

@router.get("/analytics/{user_id}")
async def analytics(user_id:int,db:AsyncSession=Depends(get_db)):
    await _user(db,user_id); plans=(await db.execute(select(WorkoutPlan).where(WorkoutPlan.user_id==user_id))).scalars().all(); recent=(await db.execute(select(WorkoutPerformance).where(WorkoutPerformance.user_id==user_id).order_by(WorkoutPerformance.completed_at.desc()).limit(10))).scalars().all(); progress=(await db.execute(select(ProgressionHistory).where(ProgressionHistory.user_id==user_id).order_by(ProgressionHistory.created_at.desc()).limit(20))).scalars().all(); hs=(await db.execute(select(TinyHabit).where(TinyHabit.user_id==user_id))).scalars().all()
    completed=sum(p.status=="completed" for p in plans)
    return {"adherence":{"planned":len(plans),"completed":completed,"rate":round(completed/len(plans),3) if plans else 0},"recent_sessions":[{"id":r.id,"completed_at":r.completed_at,"duration_minutes":r.duration_minutes,"rpe":r.perceived_exertion,"fatigue":r.fatigue,"pain":r.pain,"progression_allowed":bool(r.progression_allowed)} for r in recent],"progression":[{"exercise_id":p.exercise_id,"recommendation":p.recommendation,"created_at":p.created_at} for p in progress],"streaks":[{"habit_id":h.id,"name":h.name,"current":h.current_streak,"best":h.best_streak} for h in hs],"goal_status":{"completed_workouts":completed,"status":"in_progress" if plans else "not_started"}}
