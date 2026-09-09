import time
import os
from datetime import date, datetime
from typing import Optional
from dotenv import load_dotenv

from loguru import logger
from sqlalchemy import Column, Integer, String, Date, DateTime, Float, Text, ForeignKey, select, func, text, Index
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base, relationship

# Default subject used for chat history/profile rows created before the
# 'subject' column existed, and as a fallback when a subject isn't provided.
DEFAULT_SUBJECT = "math"

load_dotenv()


def _ensure_async_sqlite(url: str) -> str:
    if url.startswith("sqlite:///") and not url.startswith("sqlite+aiosqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


DATABASE_URL = _ensure_async_sqlite(
    os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./family_platform.db")
)

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=AsyncSession,
)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False)  # 'child' or 'parent'
    grade_level = Column(String, nullable=True)
    interests = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    learning_logs = relationship("LearningLog", back_populates="user")
    health_logs = relationship("HealthLog", back_populates="user")


class LearningLog(Base):
    __tablename__ = "learning_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    subject = Column(String, nullable=False)  # 'math' or 'english'
    topic = Column(String, nullable=False)
    score_delta = Column(Integer, nullable=False)
    mistakes_summary = Column(Text, nullable=True)
    session_duration_minutes = Column(Integer, nullable=False)
    session_date = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="learning_logs")


class HealthLog(Base):
    __tablename__ = "health_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(Date, default=date.today, nullable=False)
    workout_type = Column(String, nullable=False)
    exercises_json = Column(Text, nullable=False)
    status = Column(String, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    log_type = Column(String, nullable=True)
    details_json = Column(Text, nullable=True)
    calories = Column(Integer, nullable=True)
    protein_g = Column(Float, nullable=True)

    user = relationship("User", back_populates="health_logs")


class UserFitnessProfile(Base):
    __tablename__ = "user_fitness_profile"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    equipment = Column(Text, nullable=False)
    health_constraints = Column(Text, nullable=False)
    goal_type = Column(String, nullable=False)
    supplement_stack = Column(Text, nullable=False)
    preferred_duration_minutes = Column(Integer, nullable=False, default=30)
    training_days = Column(Text, nullable=False, default="[]")


class Exercise(Base):
    __tablename__ = "exercises"
    id = Column(Integer, primary_key=True)
    slug = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False, index=True)
    equipment = Column(String, nullable=False, index=True)
    goals_json = Column(Text, nullable=False, default="[]")
    instructions = Column(Text, nullable=False)
    video_url = Column(String, nullable=False)
    low_impact = Column(Integer, nullable=False, default=1)
    transition = Column(String, nullable=False, default="standing")
    high_impact = Column(Integer, nullable=False, default=0)


class WorkoutPlan(Base):
    __tablename__ = "workout_plans"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    planned_date = Column(Date, nullable=False, default=date.today, index=True)
    status = Column(String, nullable=False, default="planned")
    safety_notes_json = Column(Text, nullable=False, default="[]")
    duration_minutes = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class PlannedWorkoutExercise(Base):
    __tablename__ = "planned_workout_exercises"
    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("workout_plans.id"), nullable=False, index=True)
    exercise_id = Column(Integer, ForeignKey("exercises.id"), nullable=False)
    position = Column(Integer, nullable=False)
    sets = Column(Integer, nullable=False)
    reps = Column(Integer, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    rest_seconds = Column(Integer, nullable=False)
    minimum_version = Column(String, nullable=False)


class WorkoutPerformance(Base):
    __tablename__ = "workout_performances"
    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("workout_plans.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    workout_type = Column(String, nullable=False, default="planned")
    perceived_exertion = Column(Integer, nullable=False)
    fatigue = Column(Integer, nullable=False)
    pain = Column(Integer, nullable=False, default=0)
    duration_minutes = Column(Integer, nullable=False)
    notes = Column(Text, nullable=True)
    guidance = Column(Text, nullable=False)
    progression_allowed = Column(Integer, nullable=False, default=0)
    completed_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class WorkoutSet(Base):
    __tablename__ = "workout_sets"
    id = Column(Integer, primary_key=True)
    performance_id = Column(Integer, ForeignKey("workout_performances.id"), nullable=False, index=True)
    exercise_id = Column(Integer, ForeignKey("exercises.id"), nullable=True)
    set_number = Column(Integer, nullable=False)
    reps = Column(Integer, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    resistance = Column(Float, nullable=True)


class TinyHabit(Base):
    __tablename__ = "tiny_habits"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    anchor = Column(String, nullable=False)
    minimum_routine = Column(String, nullable=False)
    schedule_json = Column(Text, nullable=False, default="[]")
    current_streak = Column(Integer, nullable=False, default=0)
    best_streak = Column(Integer, nullable=False, default=0)
    last_completion = Column(Date, nullable=True)
    active = Column(Integer, nullable=False, default=1)


class TinyHabitCompletion(Base):
    __tablename__ = "tiny_habit_completions"
    id = Column(Integer, primary_key=True)
    habit_id = Column(Integer, ForeignKey("tiny_habits.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    completed_date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProgressionHistory(Base):
    __tablename__ = "progression_history"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    exercise_id = Column(Integer, ForeignKey("exercises.id"), nullable=False, index=True)
    performance_id = Column(Integer, ForeignKey("workout_performances.id"), nullable=False)
    recommendation = Column(Text, nullable=False)
    evidence_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ScheduledReport(Base):
    __tablename__ = "scheduled_reports"

    id = Column(Integer, primary_key=True, index=True)
    report_type = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow)


class MemoryFact(Base):
    __tablename__ = "memory_facts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    child_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    domain = Column(String, nullable=False, index=True)
    category = Column(String, nullable=False, default="preference")
    key = Column(String, nullable=False)
    value = Column(Text, nullable=False)
    confidence = Column(Float, nullable=False, default=1.0)
    evidence_json = Column(Text, nullable=False, default="[]")
    source_agent = Column(String, nullable=True)
    active = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class MemoryEvidence(Base):
    __tablename__ = "memory_evidence"

    id = Column(Integer, primary_key=True, index=True)
    fact_id = Column(Integer, ForeignKey("memory_facts.id"), nullable=False, index=True)
    source_type = Column(String, nullable=False, default="observation")
    source_ref = Column(String, nullable=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ParentDirective(Base):
    __tablename__ = "parent_directives"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    child_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    child_name = Column(String, nullable=True, index=True)
    subject = Column(String, nullable=True, index=True)
    message = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="active", index=True)
    priority = Column(Integer, nullable=False, default=50)
    scope = Column(String, nullable=False, default="child")
    source = Column(String, nullable=False, default="parent")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    child_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    source_agent = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    correlation_id = Column(String, nullable=False, index=True)
    causation_id = Column(Integer, ForeignKey("agent_events.id"), nullable=True)
    depth = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default="pending")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at = Column(DateTime, nullable=True)


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("agent_events.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    child_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    agent_name = Column(String, nullable=False)
    action_type = Column(String, nullable=False)
    status = Column(String, nullable=False)
    detail_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class ProfileRevision(Base):
    __tablename__ = "profile_revisions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    child_name = Column(String, nullable=False, index=True)
    subject = Column(String, nullable=False, index=True)
    previous_summary = Column(Text, nullable=True)
    new_summary = Column(Text, nullable=False)
    source = Column(String, nullable=False, default="tutor")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class RegisteredAgent(Base):
    __tablename__ = "registered_agents"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    kind = Column(String, nullable=False, default="generic")
    status = Column(String, nullable=False, default="active")
    registered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    remote_url = Column(String, nullable=True)
    secret_env = Column(String, nullable=True)
    config_json = Column(Text, nullable=False, default="{}")


class AgentCapability(Base):
    __tablename__ = "agent_capabilities"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String, ForeignKey("registered_agents.agent_id"), nullable=False, index=True)
    capability = Column(String, nullable=False)
    meta_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (Index("uq_agent_capability", "agent_id", "capability", unique=True),)


class AgentHealthSnapshot(Base):
    __tablename__ = "agent_health_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String, ForeignKey("registered_agents.agent_id"), nullable=False, index=True)
    status = Column(String, nullable=False, default="unknown")
    detail_json = Column(Text, nullable=False, default="{}")
    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (Index("ix_health_agent_recorded", "agent_id", "recorded_at"),)


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipts"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String, ForeignKey("registered_agents.agent_id"), nullable=False, index=True)
    nonce = Column(String, unique=True, index=True, nullable=False)
    signature = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at = Column(DateTime, nullable=True)


class ExternalCommandAudit(Base):
    __tablename__ = "external_command_audit"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String, ForeignKey("registered_agents.agent_id"), nullable=False, index=True)
    command_type = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    issued_by_role = Column(String, nullable=True)
    issued_by_user = Column(String, nullable=True)
    signature = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")
    response_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class AgentTelemetryCache(Base):
    __tablename__ = "agent_telemetry_cache"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String, ForeignKey("registered_agents.agent_id"), nullable=False, unique=True, index=True)
    snapshot_json = Column(Text, nullable=False, default="{}")
    cached_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)


BUILTIN_AGENTS = [
    {
        "agent_id": "koko_math",
        "name": "Koko Math Tutor",
        "description": "Interactive math mentor for children.",
        "kind": "tutor",
        "capabilities": ["chat", "session_end", "speech"],
    },
    {
        "agent_id": "koko_english",
        "name": "Koko English Tutor",
        "description": "Interactive English mentor for children.",
        "kind": "tutor",
        "capabilities": ["chat", "session_end", "speech"],
    },
    {
        "agent_id": "fitness",
        "name": "Fitness Agent",
        "description": "Family fitness planning and tracking.",
        "kind": "wellness",
        "capabilities": ["plan", "log", "analytics", "habits"],
    },
    {
        "agent_id": "nutrition",
        "name": "Nutrition Agent",
        "description": "Post-activity nutrition recommendations.",
        "kind": "wellness",
        "capabilities": ["recommend", "advise"],
    },
    {
        "agent_id": "neuraltrader",
        "name": "NeuralTrader",
        "description": "Paper trading execution agent.",
        "kind": "trading",
        "capabilities": ["paper_trade", "analytics"],
    },
    {
        "agent_id": "volatilityhunter",
        "name": "VolatilityHunter",
        "description": "Market volatility monitoring agent.",
        "kind": "trading",
        "capabilities": ["monitor", "analytics"],
    },
]


async def _seed_registered_agents(conn):
    for agent in BUILTIN_AGENTS:
        caps = agent.get("capabilities", [])
        await conn.execute(
            text(
                "INSERT INTO registered_agents (agent_id, name, description, kind, status, registered_at, config_json) "
                "VALUES (:agent_id, :name, :description, :kind, 'active', CURRENT_TIMESTAMP, '{}') "
                "ON CONFLICT(agent_id) DO UPDATE SET name=excluded.name, "
                "description=excluded.description, kind=excluded.kind, status=excluded.status"
            ),
            agent,
        )
        for cap in caps:
            await conn.execute(
                text(
                    "INSERT INTO agent_capabilities (agent_id, capability, meta_json, created_at) "
                    "VALUES (:agent_id, :cap, '{}', CURRENT_TIMESTAMP) "
                    "ON CONFLICT(agent_id, capability) DO NOTHING"
                ),
                {"agent_id": agent["agent_id"], "cap": cap},
            )


async def _migrate_main_schema(conn):
    """Additive SQLite migrations: never drop production health/fitness data."""
    profile = await _table_columns(conn, "user_fitness_profile")
    if profile and "preferred_duration_minutes" not in profile:
        await conn.execute(text("ALTER TABLE user_fitness_profile ADD COLUMN preferred_duration_minutes INTEGER NOT NULL DEFAULT 30"))
    if profile and "training_days" not in profile:
        await conn.execute(text("ALTER TABLE user_fitness_profile ADD COLUMN training_days TEXT NOT NULL DEFAULT '[]'"))
    exercises = await _table_columns(conn, "exercises")
    if exercises and "video_url" not in exercises:
        await conn.execute(text("ALTER TABLE exercises ADD COLUMN video_url VARCHAR NOT NULL DEFAULT ''"))
    health = await _table_columns(conn, "health_logs")
    legacy_columns = {
        "date": "DATE",
        "workout_type": "VARCHAR NOT NULL DEFAULT 'legacy'",
        "exercises_json": "TEXT NOT NULL DEFAULT '[]'",
        "status": "VARCHAR NOT NULL DEFAULT 'completed'",
        "notes": "TEXT",
        "created_at": "DATETIME",
    }
    for name, ddl in legacy_columns.items():
        if health and name not in health:
            await conn.execute(text(f"ALTER TABLE health_logs ADD COLUMN {name} {ddl}"))


async def _seed_exercises(conn):
    rows = [
        ("band-row", "Resistance band row", "strength", "resistance_bands", '["strength","hypertrophy"]', "Stand tall and pull with controlled tempo.", "https://www.youtube.com/results?search_query=resistance+band+row+proper+form", 1, "standing", 0),
        ("chair-squat", "Chair squat", "strength", "none", '["strength","fat_loss"]', "Sit back to a stable chair and rise slowly.", "https://www.youtube.com/results?search_query=chair+squat+proper+form", 1, "standing", 0),
        ("wall-pushup", "Wall push-up", "strength", "none", '["strength","hypertrophy"]', "Use a wall and keep a straight controlled line.", "https://www.youtube.com/results?search_query=wall+push+up+proper+form", 1, "standing", 0),
        ("band-curl", "Resistance band curl", "strength", "resistance_bands", '["strength","hypertrophy"]', "Curl without swinging.", "https://www.youtube.com/results?search_query=resistance+band+biceps+curl+proper+form", 1, "standing", 0),
        ("seated-march", "Seated march", "cardio", "none", '["fat_loss","mobility"]', "March gently while seated.", "https://www.youtube.com/results?search_query=seated+march+exercise+proper+form", 1, "seated", 0),
        ("dead-bug", "Dead bug", "core", "pilates_mat", '["strength","mobility"]', "Move opposite limbs slowly while supine.", "https://www.youtube.com/results?search_query=dead+bug+exercise+proper+form", 1, "floor", 0),
        ("jumping-jacks", "Jumping jacks", "cardio", "none", '["fat_loss"]', "Jump feet and arms apart and together.", "https://www.youtube.com/results?search_query=jumping+jacks+proper+form", 0, "standing", 1),
        ("trx-row", "TRX row", "strength", "trx", '["strength","hypertrophy"]', "Maintain body alignment and row slowly.", "https://www.youtube.com/results?search_query=TRX+row+proper+form", 1, "standing", 0),
        ("balance-board-stand", "Balance board stand", "balance", "balance_board", '["mobility","strength"]', "Stand on the balance board with soft knees and hold steady.", "https://www.youtube.com/results?search_query=balance+board+standing+exercise+beginner", 1, "standing", 0),
        ("balance-pad-squat", "Balance pad squat", "strength", "balance_pad", '["strength","mobility"]', "Squat on the balance pad with controlled descent.", "https://www.youtube.com/results?search_query=balance+pad+squat+proper+form", 1, "standing", 0),
        ("fitball-plank", "Fitball plank", "core", "fitball", '["strength","hypertrophy"]', "Hold a plank with forearms on the fitball.", "https://www.youtube.com/results?search_query=stability+ball+plank+proper+form", 1, "floor", 0),
        ("pullup-negative", "Pull-up negative", "strength", "pull_up_bar", '["strength","hypertrophy"]', "Use a stable step to reach the top position without jumping, then lower slowly.", "https://www.youtube.com/results?search_query=negative+pull+up+proper+form", 1, "standing", 0),
        ("band-pull-apart", "Resistance band pull-apart", "strength", "resistance_bands", '["strength","hypertrophy","mobility"]', "Pull the band apart at chest height with straight arms.", "https://www.youtube.com/results?search_query=resistance+band+pull+apart+proper+form", 1, "standing", 0),
        ("pilates-bridge", "Pilates bridge", "core", "pilates_mat", '["strength","mobility"]', "Lie supine and lift hips with slow control.", "https://www.youtube.com/results?search_query=pilates+bridge+proper+form", 1, "floor", 0),
        ("trx-squat", "TRX squat", "strength", "trx", '["strength","fat_loss"]', "Hold TRX handles and squat with upright torso.", "https://www.youtube.com/results?search_query=TRX+squat+proper+form", 1, "standing", 0),
        ("standing-calf-raise", "Standing calf raise", "strength", "none", '["strength","hypertrophy"]', "Rise onto toes slowly, hold briefly, and lower.", "https://www.youtube.com/results?search_query=standing+calf+raise+proper+form", 1, "standing", 0),
        ("band-lateral-walk", "Resistance band lateral walk", "strength", "resistance_bands", '["strength","fat_loss"]', "Walk sideways against band tension with soft knees.", "https://www.youtube.com/results?search_query=resistance+band+lateral+walk+proper+form", 1, "standing", 0),
        ("fitball-wall-squat", "Fitball wall squat", "strength", "fitball", '["strength","fat_loss"]', "Lean back against fitball on wall and squat to 90 degrees.", "https://www.youtube.com/results?search_query=stability+ball+wall+squat+proper+form", 1, "standing", 0),
        ("bird-dog", "Bird dog", "core", "pilates_mat", '["strength","mobility"]', "Extend opposite arm and leg from hands-and-knees position.", "https://www.youtube.com/results?search_query=bird+dog+exercise+proper+form", 1, "floor", 0),
        ("trx-chest-press", "TRX chest press", "strength", "trx", '["strength","hypertrophy"]', "Lean forward holding TRX and press out with control.", "https://www.youtube.com/results?search_query=TRX+chest+press+proper+form", 1, "standing", 0),
    ]
    for row in rows:
        await conn.execute(text("INSERT INTO exercises (slug,name,category,equipment,goals_json,instructions,video_url,low_impact,transition,high_impact) VALUES (:s,:n,:c,:e,:g,:i,:v,:l,:t,:h) ON CONFLICT(slug) DO UPDATE SET name=excluded.name, category=excluded.category, equipment=excluded.equipment, goals_json=excluded.goals_json, instructions=excluded.instructions, video_url=excluded.video_url, low_impact=excluded.low_impact, transition=excluded.transition, high_impact=excluded.high_impact"), dict(zip(("s","n","c","e","g","i","v","l","t","h"), row)))


async def init_db():
    async with engine.begin() as conn:
        await _migrate_main_schema(conn)
        await conn.run_sync(Base.metadata.create_all)
        await _seed_exercises(conn)
        await _seed_registered_agents(conn)


async def get_db():
    async with SessionLocal() as db:
        yield db


# ---------------------------------------------------------------------------
# Long-term tutor memory database
# ---------------------------------------------------------------------------

TUTOR_HISTORY_DATABASE_URL = _ensure_async_sqlite(
    os.getenv("TUTOR_HISTORY_DATABASE_URL", "sqlite+aiosqlite:///./tutor_history.db")
)

tutor_history_engine = create_async_engine(TUTOR_HISTORY_DATABASE_URL)
TutorHistorySessionLocal = async_sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=tutor_history_engine,
    class_=AsyncSession,
)
TutorHistoryBase = declarative_base()


class ChatHistory(TutorHistoryBase):
    __tablename__ = "chat_history"

    id = Column(Integer, primary_key=True, index=True)
    child_name = Column(String, nullable=False, index=True)
    subject = Column(String, nullable=False, default=DEFAULT_SUBJECT, index=True)
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    idempotency_key = Column(Text, nullable=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index(
            "uq_chat_idempotency",
            "child_name",
            "subject",
            "idempotency_key",
            unique=True,
        ),
    )


class StudentProfile(TutorHistoryBase):
    __tablename__ = "student_profiles"

    child_name = Column(String, primary_key=True, index=True)
    subject = Column(String, primary_key=True, default=DEFAULT_SUBJECT)
    profile_summary = Column(Text, nullable=True)
    total_points = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ParentFeedback(TutorHistoryBase):
    __tablename__ = "parent_feedback"

    id = Column(Integer, primary_key=True, index=True)
    child_name = Column(String, index=True, nullable=True)
    subject = Column(String, index=True, nullable=True)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


async def _table_columns(conn, table_name: str):
    """Return the set of column names currently present in a SQLite table
    (empty set if the table doesn't exist yet).
    """
    result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
    return {row[1] for row in result.fetchall()}


async def _migrate_tutor_history_schema(conn):
    """Ensure chat_history and student_profiles have the 'subject' column so
    math and English tutor sessions are stored separately.

    SQLite can't easily change a table's primary key (needed for
    student_profiles, which becomes a composite (child_name, subject) key),
    so for both tables we simply drop and let create_all() recreate them
    with the new schema if the old 'subject'-less schema is detected. This
    is an acceptable one-time reset for this local, single-family app.
    """
    for table_name in ("chat_history", "student_profiles"):
        columns = await _table_columns(conn, table_name)
        if columns and "subject" not in columns:
            logger.warning(
                f"Old '{table_name}' schema detected without 'subject' column; "
                "dropping and recreating table to separate math/English data."
            )
            await conn.execute(text(f"DROP TABLE {table_name}"))

    # Add the total_points column to the existing student_profiles table if it
    # is missing. This preserves existing profile summaries and chat history.
    sp_columns = await _table_columns(conn, "student_profiles")
    if sp_columns and "total_points" not in sp_columns:
        logger.warning("Adding 'total_points' column to student_profiles")
        await conn.execute(
            text("ALTER TABLE student_profiles ADD COLUMN total_points INTEGER DEFAULT 0")
        )

    # Add the idempotency_key column to the existing chat_history table so that
    # duplicate chat/audio requests can be detected and replayed safely without
    # calling the LLM or awarding points twice.
    ch_columns = await _table_columns(conn, "chat_history")
    if ch_columns and "idempotency_key" not in ch_columns:
        logger.warning("Adding 'idempotency_key' column to chat_history")
        await conn.execute(text("ALTER TABLE chat_history ADD COLUMN idempotency_key TEXT"))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_idempotency "
                "ON chat_history(child_name, subject, idempotency_key)"
            )
        )


async def init_tutor_history_db():
    async with tutor_history_engine.begin() as conn:
        await _migrate_tutor_history_schema(conn)
        await conn.run_sync(TutorHistoryBase.metadata.create_all)


async def get_chat_history(child_name: str, subject: str = DEFAULT_SUBJECT, limit: int = 10):
    """Return the last N chat messages for a child within a specific subject,
    oldest first. Filtering by subject keeps math and English conversations
    from leaking into each other's context.
    """
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        result = await db.execute(
            select(ChatHistory)
            .where(ChatHistory.child_name == child_name, ChatHistory.subject == subject)
            .order_by(ChatHistory.timestamp.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
    duration = time.perf_counter() - start
    logger.debug(
        "get_chat_history completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
        subject=subject,
    )
    return [{"role": r.role, "content": r.content, "timestamp": r.timestamp} for r in reversed(rows)]


async def append_chat_messages(
    child_name: str,
    subject: str,
    conversation_history,
    new_reply: str,
    idempotency_key: Optional[str] = None,
):
    """Persist the new tail of the conversation plus the latest assistant reply.
    Uses the current row count (scoped to this subject) to avoid duplicates
    when the frontend resends the full conversation history. The assistant
    reply is tagged with the idempotency key of the request so that repeated
    requests with the same key can be safely replayed without calling the LLM.
    """
    if not child_name:
        return
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        count_result = await db.execute(
            select(func.count()).where(
                ChatHistory.child_name == child_name, ChatHistory.subject == subject
            )
        )
        current_count = count_result.scalar()
        new_user_messages = conversation_history[current_count:]
        for msg in new_user_messages:
            db.add(
                ChatHistory(
                    child_name=child_name,
                    subject=subject,
                    role=msg.get("role", "user"),
                    content=msg.get("content", ""),
                )
            )
        db.add(
            ChatHistory(
                child_name=child_name,
                subject=subject,
                role="assistant",
                content=new_reply,
                idempotency_key=idempotency_key,
            )
        )
        await db.commit()
    duration = time.perf_counter() - start
    logger.debug(
        "append_chat_messages completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
        subject=subject,
    )


async def get_chat_by_idempotency_key(
    child_name: str, subject: str, idempotency_key: str
):
    """Return a previous assistant reply for the given idempotency key, or None.
    """
    if not idempotency_key:
        return None
    async with TutorHistorySessionLocal() as db:
        result = await db.execute(
            select(ChatHistory)
            .where(
                ChatHistory.child_name == child_name,
                ChatHistory.subject == subject,
                ChatHistory.idempotency_key == idempotency_key,
                ChatHistory.role == "assistant",
            )
            .order_by(ChatHistory.timestamp.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
    return row


async def get_student_profile_summary(child_name: str, subject: str = DEFAULT_SUBJECT):
    """Return the persisted learning profile summary for a child within a
    specific subject, or None.
    """
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        result = await db.execute(
            select(StudentProfile).where(
                StudentProfile.child_name == child_name, StudentProfile.subject == subject
            )
        )
        p = result.scalar_one_or_none()
    duration = time.perf_counter() - start
    logger.debug(
        "get_student_profile_summary completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
        subject=subject,
    )
    return p.profile_summary if p else None


async def update_student_profile_summary(child_name: str, subject: str, summary: str):
    """Create or update the learning profile summary for a child within a
    specific subject.
    """
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        result = await db.execute(
            select(StudentProfile).where(
                StudentProfile.child_name == child_name, StudentProfile.subject == subject
            )
        )
        p = result.scalar_one_or_none()
        previous_summary = p.profile_summary if p else None
        if p:
            p.profile_summary = summary
            p.updated_at = datetime.utcnow()
        else:
            db.add(
                StudentProfile(
                    child_name=child_name,
                    subject=subject,
                    profile_summary=summary,
                    updated_at=datetime.utcnow(),
                )
            )
        await db.commit()
    async with SessionLocal() as main_db:
        user_result = await main_db.execute(select(User).where(User.name == child_name))
        user = user_result.scalar_one_or_none()
        main_db.add(
            ProfileRevision(
                user_id=user.id if user else None,
                child_name=child_name,
                subject=subject,
                previous_summary=previous_summary,
                new_summary=summary,
            )
        )
        await main_db.commit()
    duration = time.perf_counter() - start
    logger.debug(
        "update_student_profile_summary completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
        subject=subject,
    )


async def add_parent_feedback(child_name: Optional[str], subject: Optional[str], message: str):
    """Store legacy feedback and its durable directive representation."""
    async with TutorHistorySessionLocal() as db:
        db.add(ParentFeedback(child_name=child_name, subject=subject, message=message))
        await db.commit()
    async with SessionLocal() as db:
        user = None
        if child_name:
            result = await db.execute(select(User).where(User.name == child_name))
            user = result.scalar_one_or_none()
        db.add(
            ParentDirective(
                user_id=user.id if user else None,
                child_id=user.id if user else None,
                child_name=child_name,
                subject=subject,
                message=message,
                scope="child" if child_name else "family",
            )
        )
        await db.commit()


async def get_latest_parent_feedback(child_name: str, subject: str = None, limit: int = 3):
    """Return the latest parent feedback messages that apply to a child/subject.
    Messages with no child_name apply to all children, and messages with no
    subject apply to all subjects.
    """
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        query = (
            select(ParentFeedback)
            .where(
                (ParentFeedback.child_name == child_name) | (ParentFeedback.child_name.is_(None))
            )
        )
        if subject:
            query = query.where(
                (ParentFeedback.subject == subject) | (ParentFeedback.subject.is_(None))
            )
        result = await db.execute(
            query.order_by(ParentFeedback.created_at.desc()).limit(limit)
        )
        rows = result.scalars().all()
    duration = time.perf_counter() - start
    logger.debug(
        "get_latest_parent_feedback completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
        subject=subject,
    )
    return [r.message for r in rows]


async def add_student_points(child_name: str, subject: str, points: int) -> int:
    """Add points to a child's subject-specific profile and return the new total."""
    points = max(0, int(points))
    async with TutorHistorySessionLocal() as db:
        result = await db.execute(
            select(StudentProfile).where(
                StudentProfile.child_name == child_name, StudentProfile.subject == subject
            )
        )
        p = result.scalar_one_or_none()
        if p:
            p.total_points = (p.total_points or 0) + points
            p.updated_at = datetime.utcnow()
            new_total = p.total_points
        else:
            new_total = points
            db.add(
                StudentProfile(
                    child_name=child_name,
                    subject=subject,
                    profile_summary="",
                    total_points=points,
                    updated_at=datetime.utcnow(),
                )
            )
        await db.commit()
    logger.info(
        "Student points updated",
        child_name=child_name,
        subject=subject,
        points_earned=points,
        total_points=new_total,
    )
    return new_total


async def get_student_points(child_name: str, subject: str = DEFAULT_SUBJECT) -> int:
    """Return a child's total points for a subject, defaulting to 0."""
    async with TutorHistorySessionLocal() as db:
        result = await db.execute(
            select(StudentProfile.total_points).where(
                StudentProfile.child_name == child_name, StudentProfile.subject == subject
            )
        )
        row = result.one_or_none()
        return (row[0] if row else 0) or 0
