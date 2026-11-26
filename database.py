import time
import os
from datetime import date, datetime
from typing import Optional
from dotenv import load_dotenv

from loguru import logger
from sqlalchemy import Column, Integer, String, Date, DateTime, Float, Text, ForeignKey, select, text, Index
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
    training_mode = Column(String, nullable=False, default="family_fitness")
    age = Column(Integer, nullable=True)
    height_cm = Column(Float, nullable=True)
    weight_kg = Column(Float, nullable=True)
    experience_level = Column(String, nullable=False, default="beginner")


class Exercise(Base):
    __tablename__ = "exercises"
    id = Column(Integer, primary_key=True)
    slug = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False, index=True)
    muscle_group = Column(String, nullable=False, default="full_body", index=True)
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
    adaptations_json = Column(Text, nullable=False, default="[]")
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
    target_resistance = Column(Float, nullable=True)
    target_rir = Column(Float, nullable=True)
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
    rir = Column(Float, nullable=True)


class WorkoutReadiness(Base):
    __tablename__ = "workout_readiness"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    energy = Column(Integer, nullable=False)
    sleep_quality = Column(Integer, nullable=False)
    soreness = Column(Integer, nullable=False)
    pain = Column(Integer, nullable=False, default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class BodyMetric(Base):
    __tablename__ = "body_metrics"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    weight_kg = Column(Float, nullable=True)
    waist_cm = Column(Float, nullable=True)
    chest_cm = Column(Float, nullable=True)
    arm_cm = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    measured_at = Column(Date, nullable=False, default=date.today, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


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


class DailyReportLock(Base):
    __tablename__ = "daily_report_locks"

    # Primary key on the local calendar date gives us an atomic, cross-process
    # guard. The first process to insert a row for today wins the right to send
    # the daily summary; all other concurrent attempts get an IntegrityError.
    report_date = Column(Date, primary_key=True)
    status = Column(String, nullable=False, default="running")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    sent_at = Column(DateTime, nullable=True)


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
    # Built-in agents only: Python module path (e.g. "fitness_agent" or "agents.messenger_agent")
    # and the attribute that exposes the FastAPI APIRouter (default "router").
    module_path = Column(String, nullable=True)
    router_attr = Column(String, nullable=True, default="router")
    is_builtin = Column(Integer, nullable=False, default=0)
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


class ScoutItem(Base):
    __tablename__ = "scout_items"

    id = Column(Integer, primary_key=True, index=True)
    # e.g. "telegram:@channel", "rss:https://feed", "discord:123456789"
    source = Column(String, nullable=False, index=True)
    external_id = Column(String, nullable=False)
    author = Column(String, nullable=True)
    title = Column(String, nullable=True)
    content = Column(Text, nullable=False, default="")
    url = Column(String, nullable=True)
    published_at = Column(DateTime, nullable=True)
    collected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Flipped to 1 once the item has been included in a digest that was sent.
    digested = Column(Integer, nullable=False, default=0, index=True)

    __table_args__ = (Index("uq_scout_item_source_ext", "source", "external_id", unique=True),)


class ScoutSourceState(Base):
    __tablename__ = "scout_source_state"

    source = Column(String, primary_key=True)
    last_seen_id = Column(String, nullable=True)
    last_run_at = Column(DateTime, nullable=True)


BUILTIN_AGENTS = [
    {
        "agent_id": "koko_math",
        "name": "Koko Math Tutor",
        "description": "Interactive math mentor for children.",
        "kind": "tutor",
        "capabilities": ["chat", "session_end", "speech"],
        "is_builtin": 1,
        # Tutors are wired directly through legacy /api/tutor/* endpoints.
        "module_path": None,
        "router_attr": None,
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "koko_english",
        "name": "Koko English Tutor",
        "description": "Interactive English mentor for children.",
        "kind": "tutor",
        "capabilities": ["chat", "session_end", "speech"],
        "is_builtin": 1,
        "module_path": None,
        "router_attr": None,
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "fitness",
        "name": "Coach",
        "description": "Personal fitness coach that learns your history, avoids exercises that caused pain, and plans workouts just for you.",
        "kind": "wellness",
        "capabilities": ["plan", "log", "analytics", "habits"],
        "is_builtin": 1,
        "module_path": "fitness_agent",
        "router_attr": "router",
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "nutrition",
        "name": "Nutrition Agent",
        "description": "Post-activity nutrition recommendations.",
        "kind": "wellness",
        "capabilities": ["recommend", "advise"],
        "is_builtin": 1,
        "module_path": "agents.nutrition_agent",
        "router_attr": "router",
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "meal_coach",
        "name": "Meal Coach",
        "description": "Coach-led family meal planning: draft, approve, publish, and honest eating ledger.",
        "kind": "wellness",
        "capabilities": ["plan", "publish", "log", "catalog"],
        "is_builtin": 1,
        "module_path": "agents.nutrition_coach_agent",
        "router_attr": "router",
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "orchestrator",
        "name": "Orchestrator",
        "description": "Meta-agent that coordinates other agents, schedules reports, and escalates to parents.",
        "kind": "platform",
        "capabilities": ["schedule", "summarize", "escalate"],
        "is_builtin": 1,
        "module_path": "orchestrator",
        "router_attr": "router",
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "messenger",
        "name": "Messenger",
        "description": "Cross-channel messaging agent for Telegram, email, and future channels.",
        "kind": "platform",
        "capabilities": ["send_message"],
        "is_builtin": 1,
        "module_path": "agents.messenger_agent",
        "router_attr": "router",
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "data",
        "name": "Data Steward",
        "description": "Read-only data and memory query agent for the platform.",
        "kind": "platform",
        "capabilities": ["query", "summarize_data"],
        "is_builtin": 1,
        "module_path": "agents.data_agent",
        "router_attr": "router",
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "scout_ideas",
        "name": "Scout Ideas",
        "description": "Scans subscribed Telegram channels, RSS feeds, and Discord channels for investment ideas and sends periodic digests.",
        "kind": "platform",
        "capabilities": ["collect", "digest", "alert"],
        "is_builtin": 1,
        "module_path": "agents.scout_ideas_agent",
        "router_attr": "router",
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "neuraltrader",
        "name": "NeuralTrader",
        "description": "Paper trading execution agent.",
        "kind": "trading",
        "capabilities": ["paper_trade", "analytics"],
        "is_builtin": 0,
        "module_path": None,
        "router_attr": None,
        "remote_url": None,
        "secret_env": None,
    },
    {
        "agent_id": "volatilityhunter",
        "name": "VolatilityHunter",
        "description": "Market volatility monitoring agent.",
        "kind": "trading",
        "capabilities": ["monitor", "analytics"],
        "is_builtin": 0,
        "module_path": None,
        "router_attr": None,
        "remote_url": None,
        "secret_env": None,
    },
]


async def _seed_registered_agents(conn):
    for agent in BUILTIN_AGENTS:
        caps = agent.get("capabilities", [])
        params = {
            "agent_id": agent["agent_id"],
            "name": agent["name"],
            "description": agent.get("description"),
            "kind": agent["kind"],
            "remote_url": agent.get("remote_url"),
            "secret_env": agent.get("secret_env"),
            "module_path": agent.get("module_path"),
            "router_attr": agent.get("router_attr"),
            "is_builtin": agent.get("is_builtin", 0),
        }
        await conn.execute(
            text(
                "INSERT INTO registered_agents ("
                "agent_id, name, description, kind, status, registered_at, "
                "remote_url, secret_env, module_path, router_attr, is_builtin, config_json"
                ") VALUES ("
                ":agent_id, :name, :description, :kind, 'active', CURRENT_TIMESTAMP, "
                ":remote_url, :secret_env, :module_path, :router_attr, :is_builtin, '{}'"
                ") ON CONFLICT(agent_id) DO UPDATE SET "
                "name=excluded.name, description=excluded.description, kind=excluded.kind, "
                "status=excluded.status, module_path=excluded.module_path, "
                "router_attr=excluded.router_attr, is_builtin=excluded.is_builtin"
            ),
            params,
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
    agents = await _table_columns(conn, "registered_agents")
    if agents:
        if "module_path" not in agents:
            await conn.execute(text("ALTER TABLE registered_agents ADD COLUMN module_path VARCHAR"))
        if "router_attr" not in agents:
            await conn.execute(text("ALTER TABLE registered_agents ADD COLUMN router_attr VARCHAR DEFAULT 'router'"))
        if "is_builtin" not in agents:
            await conn.execute(text("ALTER TABLE registered_agents ADD COLUMN is_builtin INTEGER NOT NULL DEFAULT 0"))
    profile = await _table_columns(conn, "user_fitness_profile")
    if profile and "preferred_duration_minutes" not in profile:
        await conn.execute(text("ALTER TABLE user_fitness_profile ADD COLUMN preferred_duration_minutes INTEGER NOT NULL DEFAULT 30"))
    if profile and "training_days" not in profile:
        await conn.execute(text("ALTER TABLE user_fitness_profile ADD COLUMN training_days TEXT NOT NULL DEFAULT '[]'"))
    profile_columns = {
        "training_mode": "VARCHAR NOT NULL DEFAULT 'family_fitness'",
        "age": "INTEGER",
        "height_cm": "FLOAT",
        "weight_kg": "FLOAT",
        "experience_level": "VARCHAR NOT NULL DEFAULT 'beginner'",
    }
    for name, ddl in profile_columns.items():
        if profile and name not in profile:
            await conn.execute(text(f"ALTER TABLE user_fitness_profile ADD COLUMN {name} {ddl}"))
    workout_sets = await _table_columns(conn, "workout_sets")
    if workout_sets and "rir" not in workout_sets:
        await conn.execute(text("ALTER TABLE workout_sets ADD COLUMN rir FLOAT"))
    planned_sets = await _table_columns(conn, "planned_workout_exercises")
    if planned_sets and "target_resistance" not in planned_sets:
        await conn.execute(text("ALTER TABLE planned_workout_exercises ADD COLUMN target_resistance FLOAT"))
    if planned_sets and "target_rir" not in planned_sets:
        await conn.execute(text("ALTER TABLE planned_workout_exercises ADD COLUMN target_rir FLOAT"))
    exercises = await _table_columns(conn, "exercises")
    if exercises and "video_url" not in exercises:
        await conn.execute(text("ALTER TABLE exercises ADD COLUMN video_url VARCHAR NOT NULL DEFAULT ''"))
    if exercises and "muscle_group" not in exercises:
        await conn.execute(text("ALTER TABLE exercises ADD COLUMN muscle_group VARCHAR NOT NULL DEFAULT 'full_body'"))
    plans = await _table_columns(conn, "workout_plans")
    if plans and "adaptations_json" not in plans:
        await conn.execute(text("ALTER TABLE workout_plans ADD COLUMN adaptations_json TEXT NOT NULL DEFAULT '[]'"))
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
    # Curated short-form YouTube links to proper-form demonstrations.
    # Format: https://youtu.be/<video_id>
    rows = [
        ("band-row", "Resistance band row", "strength", "resistance_bands", '["strength","hypertrophy"]', "Stand tall and pull with controlled tempo.", "https://youtu.be/lHuGxZZ09nY", 1, "standing", 0),
        ("chair-squat", "Chair squat", "strength", "none", '["strength","fat_loss"]', "Sit back to a stable chair and rise slowly.", "https://youtu.be/fNpFifF4PLU", 1, "standing", 0),
        ("wall-pushup", "Wall push-up", "strength", "none", '["strength","hypertrophy"]', "Use a wall and keep a straight controlled line.", "https://youtu.be/QpMTk21EmaM", 1, "standing", 0),
        ("band-curl", "Resistance band curl", "strength", "resistance_bands", '["strength","hypertrophy"]', "Curl without swinging.", "https://youtu.be/pXS-fSPWpk8", 1, "standing", 0),
        ("seated-march", "Seated march", "cardio", "none", '["fat_loss","mobility"]', "March gently while seated.", "https://youtu.be/OfH6a_pZ4dE", 1, "seated", 0),
        ("dead-bug", "Dead bug", "core", "pilates_mat", '["strength","mobility"]', "Move opposite limbs slowly while supine.", "https://youtu.be/GbSC02oU3To", 1, "floor", 0),
        ("jumping-jacks", "Jumping jacks", "cardio", "none", '["fat_loss"]', "Jump feet and arms apart and together.", "https://youtu.be/iSSAk4XCsRA", 0, "standing", 1),
        ("trx-row", "TRX row", "strength", "trx", '["strength","hypertrophy"]', "Maintain body alignment and row slowly.", "https://youtu.be/N_14s8zFOms", 1, "standing", 0),
        ("balance-board-stand", "Balance board stand", "balance", "balance_board", '["mobility","strength"]', "Stand on the balance board with soft knees and hold steady.", "https://youtu.be/KTYF2ULADzg", 1, "standing", 0),
        ("balance-pad-squat", "Balance pad squat", "strength", "balance_pad", '["strength","mobility"]', "Squat on the balance pad with controlled descent.", "https://youtu.be/uX1VushX7PM", 1, "standing", 0),
        ("fitball-plank", "Fitball plank", "core", "fitball", '["strength","hypertrophy"]', "Hold a plank with forearms on the fitball.", "https://youtu.be/AqnN2_gt9Ko", 1, "floor", 0),
        ("pullup-negative", "Pull-up negative", "strength", "pull_up_bar", '["strength","hypertrophy"]', "Use a stable step to reach the top position without jumping, then lower slowly.", "https://youtu.be/gbPURTSxQLY", 1, "standing", 0),
        ("band-pull-apart", "Resistance band pull-apart", "strength", "resistance_bands", '["strength","hypertrophy","mobility"]', "Pull the band apart at chest height with straight arms.", "https://youtu.be/vXcji-UoreQ", 1, "standing", 0),
        ("pilates-bridge", "Pilates bridge", "core", "pilates_mat", '["strength","mobility"]', "Lie supine and lift hips with slow control.", "https://youtu.be/j6A3YhGjsr8", 1, "floor", 0),
        ("trx-squat", "TRX squat", "strength", "trx", '["strength","fat_loss"]', "Hold TRX handles and squat with upright torso.", "https://youtu.be/gvtkh63UeRg", 1, "standing", 0),
        ("standing-calf-raise", "Standing calf raise", "strength", "none", '["strength","hypertrophy"]', "Rise onto toes slowly, hold briefly, and lower.", "https://youtu.be/gwLzBJYoWlI", 1, "standing", 0),
        ("band-lateral-walk", "Resistance band lateral walk", "strength", "resistance_bands", '["strength","fat_loss"]', "Walk sideways against band tension with soft knees.", "https://youtu.be/M5uxEQH5BUM", 1, "standing", 0),
        ("fitball-wall-squat", "Fitball wall squat", "strength", "fitball", '["strength","fat_loss"]', "Lean back against fitball on wall and squat to 90 degrees.", "https://youtu.be/rdpFZtaIwKc", 1, "standing", 0),
        ("bird-dog", "Bird dog", "core", "pilates_mat", '["strength","mobility"]', "Extend opposite arm and leg from hands-and-knees position.", "https://youtu.be/xEDnlOxeJH4", 1, "floor", 0),
        ("trx-chest-press", "TRX chest press", "strength", "trx", '["strength","hypertrophy"]', "Lean forward holding TRX and press out with control.", "https://youtu.be/uutnRHSbFgM", 1, "standing", 0),
    ]
    muscle_groups = {
        "band-row": "back", "chair-squat": "legs", "wall-pushup": "chest",
        "band-curl": "arms", "seated-march": "cardio", "dead-bug": "core",
        "jumping-jacks": "cardio", "trx-row": "back", "balance-board-stand": "balance",
        "balance-pad-squat": "legs", "fitball-plank": "core", "pullup-negative": "back",
        "band-pull-apart": "shoulders", "pilates-bridge": "glutes", "trx-squat": "legs",
        "standing-calf-raise": "legs", "band-lateral-walk": "glutes",
        "fitball-wall-squat": "legs", "bird-dog": "core", "trx-chest-press": "chest",
    }
    for row in rows:
        await conn.execute(text("INSERT INTO exercises (slug,name,category,muscle_group,equipment,goals_json,instructions,video_url,low_impact,transition,high_impact) VALUES (:s,:n,:c,:mg,:e,:g,:i,:v,:l,:t,:h) ON CONFLICT(slug) DO UPDATE SET name=excluded.name, category=excluded.category, muscle_group=excluded.muscle_group, equipment=excluded.equipment, goals_json=excluded.goals_json, instructions=excluded.instructions, video_url=excluded.video_url, low_impact=excluded.low_impact, transition=excluded.transition, high_impact=excluded.high_impact"), {**dict(zip(("s","n","c","e","g","i","v","l","t","h"), row)), "mg": muscle_groups.get(row[0], "full_body")})
        await conn.execute(
            text("UPDATE exercises SET muscle_group = :mg WHERE slug = :s"),
            {"mg": muscle_groups.get(row[0], "full_body"), "s": row[0]},
        )


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


class TutorSkillMastery(TutorHistoryBase):
    __tablename__ = "tutor_skill_mastery"

    id = Column(Integer, primary_key=True, index=True)
    child_name = Column(String, nullable=False, index=True)
    subject = Column(String, nullable=False, index=True)
    skill_id = Column(String, nullable=False, index=True)
    mastery = Column(Float, nullable=False, default=0.0)
    attempts = Column(Integer, nullable=False, default=0)
    correct_attempts = Column(Integer, nullable=False, default=0)
    independent_correct = Column(Integer, nullable=False, default=0)
    current_difficulty = Column(Integer, nullable=False, default=1)
    last_practiced_at = Column(DateTime, nullable=True)
    next_review_at = Column(DateTime, nullable=True, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("uq_tutor_skill_mastery", "child_name", "subject", "skill_id", unique=True),
    )


class EnglishLessonProgress(TutorHistoryBase):
    """One row per child per lesson: stars earned and whether it was finished.

    The same lesson can be replayed; the best star count is what sticks.
    """
    __tablename__ = "english_lesson_progress"

    id = Column(Integer, primary_key=True)
    child_name = Column(String, nullable=False, index=True)
    lesson_id = Column(String, nullable=False)
    stars = Column(Integer, nullable=False, default=0)
    completed = Column(Integer, nullable=False, default=0)
    opened_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("uq_english_lesson_progress", "child_name", "lesson_id", unique=True),
    )


class EnglishChildWallet(TutorHistoryBase):
    """Star bank and the parent's reward settings for the English courses.

    Stars accumulate across lessons (one per correct slide, plus finish and
    unit bonuses). reward_every=0 means the parent has not set a gift cycle.
    """
    __tablename__ = "english_child_wallet"

    child_name = Column(String, primary_key=True)
    total_stars = Column(Integer, nullable=False, default=0)
    reward_every = Column(Integer, nullable=False, default=0)
    reward_text = Column(String, nullable=False, default="")
    rewards_pending = Column(Integer, nullable=False, default=0)
    rewards_claimed = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EnglishCoursePlacement(TutorHistoryBase):
    """Which English course the parent put this child in.

    The course is a parent decision, never inferred from age or grade, and any
    child may be placed in any course.
    """
    __tablename__ = "english_course_placement"

    child_name = Column(String, primary_key=True)
    course_id = Column(String, nullable=False)
    daily_lesson_limit = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TutorPracticeAttempt(TutorHistoryBase):
    __tablename__ = "tutor_practice_attempts"

    id = Column(Integer, primary_key=True, index=True)
    child_name = Column(String, nullable=False, index=True)
    subject = Column(String, nullable=False, index=True)
    skill_id = Column(String, nullable=False, index=True)
    correct = Column(Integer, nullable=False)
    hints_used = Column(Integer, nullable=False, default=0)
    difficulty = Column(Integer, nullable=False, default=1)
    idempotency_key = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("uq_tutor_practice_attempt", "child_name", "subject", "idempotency_key", unique=True),
    )


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

    # Track when a lesson was last opened and how long completing it took,
    # so the parent can see a 35-second "finish" for what it is.
    lp_columns = await _table_columns(conn, "english_lesson_progress")
    if lp_columns:
        if "opened_at" not in lp_columns:
            logger.warning("Adding 'opened_at' column to english_lesson_progress")
            await conn.execute(
                text("ALTER TABLE english_lesson_progress ADD COLUMN opened_at DATETIME")
            )
        if "duration_seconds" not in lp_columns:
            logger.warning("Adding 'duration_seconds' column to english_lesson_progress")
            await conn.execute(
                text("ALTER TABLE english_lesson_progress ADD COLUMN duration_seconds INTEGER")
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
            # A user turn and KOKO's reply are committed with the SAME
            # timestamp - the id tiebreak keeps the reply (inserted last)
            # on top, or a pending-question check reads the wrong tail.
            .order_by(ChatHistory.timestamp.desc(), ChatHistory.id.desc())
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

    The frontend only sends the *current session's* messages, while this table
    holds the full history across all sessions — so a positional slice against
    the total row count silently drops the child's messages forever. Instead we
    persist the trailing messages that come after the last assistant turn in
    the payload: those are exactly the new user messages for this turn. The
    assistant reply is tagged with the idempotency key of the request so that
    repeated requests with the same key can be safely replayed without calling
    the LLM.
    """
    if not child_name:
        return
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        tail = list(conversation_history or [])
        last_assistant_idx = -1
        for idx, msg in enumerate(tail):
            if (msg.get("role") or "") == "assistant":
                last_assistant_idx = idx
        new_user_messages = tail[last_assistant_idx + 1:]
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


# Skill curriculum shared by the API layer and the tutors' lesson prep.
# Levels gate eligibility by grade (grade_level_cap); mastery data lives in
# TutorSkillMastery rows per child.
TUTOR_CURRICULUM = {
    "math": [
        {"skill_id": "math.addition.basic", "name": "חיבור", "level": 1},
        {"skill_id": "math.subtraction.basic", "name": "חיסור", "level": 1},
        {"skill_id": "math.multiplication.basic", "name": "כפל", "level": 2},
        {"skill_id": "math.division.basic", "name": "חילוק", "level": 2},
        {"skill_id": "math.fractions.basic", "name": "שברים", "level": 3},
        {"skill_id": "math.word_problems.basic", "name": "בעיות מילוליות", "level": 3},
    ],
    "english": [
        {"skill_id": "english.vocabulary.basic", "name": "אוצר מילים", "level": 1},
        {"skill_id": "english.sentences.basic", "name": "בניית משפטים", "level": 1},
        {"skill_id": "english.present_simple", "name": "Present Simple", "level": 2},
        {"skill_id": "english.past_simple", "name": "Past Simple", "level": 2},
        {"skill_id": "english.reading.basic", "name": "הבנת הנקרא", "level": 3},
        {"skill_id": "english.writing.basic", "name": "כתיבה", "level": 3},
    ],
}


def grade_level_cap(grade_level: Optional[str]) -> int:
    """Highest curriculum level a child's grade may see (gan/א=1, ב-ג=2, ד+=3)."""
    value = (grade_level or "").lower()
    if any(token in value for token in ("גן", "kindergarten", "כיתה א", "1st", "grade 1")):
        return 1
    if any(token in value for token in ("כיתה ב", "כיתה ג", "2nd", "3rd", "grade 2", "grade 3")):
        return 2
    return 3


async def get_skill_mastery(child_name: str, subject: str = DEFAULT_SUBJECT) -> dict:
    """All mastery rows for a child+subject, keyed by skill_id."""
    async with TutorHistorySessionLocal() as db:
        rows = (await db.execute(
            select(TutorSkillMastery).where(
                TutorSkillMastery.child_name == child_name,
                TutorSkillMastery.subject == subject,
            )
        )).scalars().all()
    return {row.skill_id: row for row in rows}


async def record_speech_mastery(child_name: str, subject: str, skill_id: str, correct: bool):
    """Upsert a mastery row for an internally-graded signal (e.g. spoken
    sentence accuracy) that isn't part of the public curriculum, so lesson
    prep can adapt the channel difficulty per child."""
    async with TutorHistorySessionLocal() as db:
        row = (await db.execute(
            select(TutorSkillMastery).where(
                TutorSkillMastery.child_name == child_name,
                TutorSkillMastery.subject == subject,
                TutorSkillMastery.skill_id == skill_id,
            )
        )).scalar_one_or_none()
        if row is None:
            row = TutorSkillMastery(child_name=child_name, subject=subject, skill_id=skill_id)
            db.add(row)
        delta = 0.15 if correct else -0.08
        row.mastery = max(0.0, min(1.0, float(row.mastery or 0.0) + delta))
        row.attempts = int(row.attempts or 0) + 1
        row.correct_attempts = int(row.correct_attempts or 0) + int(correct)
        row.last_practiced_at = datetime.utcnow()
        await db.commit()


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
