import time
import os
from datetime import datetime
from dotenv import load_dotenv

from loguru import logger
from sqlalchemy import Column, Integer, String, DateTime, Float, Text, ForeignKey, select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base, relationship

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
    created_at = Column(DateTime, default=datetime.utcnow)

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
    log_type = Column(String, nullable=False)  # 'workout' or 'nutrition'
    details_json = Column(Text, nullable=True)
    calories = Column(Integer, nullable=True)
    protein_g = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="health_logs")


class ScheduledReport(Base):
    __tablename__ = "scheduled_reports"

    id = Column(Integer, primary_key=True, index=True)
    report_type = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)


class StudentProfile(TutorHistoryBase):
    __tablename__ = "student_profiles"

    child_name = Column(String, primary_key=True, index=True)
    profile_summary = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


async def init_tutor_history_db():
    async with tutor_history_engine.begin() as conn:
        await conn.run_sync(TutorHistoryBase.metadata.create_all)


async def get_chat_history(child_name: str, limit: int = 10):
    """Return the last N chat messages for a child, oldest first."""
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        result = await db.execute(
            select(ChatHistory)
            .where(ChatHistory.child_name == child_name)
            .order_by(ChatHistory.timestamp.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
    duration = time.perf_counter() - start
    logger.debug(
        "get_chat_history completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
    )
    return [{"role": r.role, "content": r.content, "timestamp": r.timestamp} for r in reversed(rows)]


async def append_chat_messages(child_name: str, conversation_history, new_reply: str):
    """Persist the new tail of the conversation plus the latest assistant reply.
    Uses the current row count to avoid duplicates when the frontend resends the
    full conversation history.
    """
    if not child_name:
        return
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        count_result = await db.execute(
            select(func.count()).where(ChatHistory.child_name == child_name)
        )
        current_count = count_result.scalar()
        new_user_messages = conversation_history[current_count:]
        for msg in new_user_messages:
            db.add(
                ChatHistory(
                    child_name=child_name,
                    role=msg.get("role", "user"),
                    content=msg.get("content", ""),
                )
            )
        db.add(
            ChatHistory(
                child_name=child_name,
                role="assistant",
                content=new_reply,
            )
        )
        await db.commit()
    duration = time.perf_counter() - start
    logger.debug(
        "append_chat_messages completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
    )


async def get_student_profile_summary(child_name: str):
    """Return the persisted learning profile summary for a child, or None."""
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        result = await db.execute(
            select(StudentProfile).where(StudentProfile.child_name == child_name)
        )
        p = result.scalar_one_or_none()
    duration = time.perf_counter() - start
    logger.debug(
        "get_student_profile_summary completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
    )
    return p.profile_summary if p else None


async def update_student_profile_summary(child_name: str, summary: str):
    """Create or update the learning profile summary for a child."""
    start = time.perf_counter()
    async with TutorHistorySessionLocal() as db:
        result = await db.execute(
            select(StudentProfile).where(StudentProfile.child_name == child_name)
        )
        p = result.scalar_one_or_none()
        if p:
            p.profile_summary = summary
            p.updated_at = datetime.utcnow()
        else:
            db.add(
                StudentProfile(
                    child_name=child_name,
                    profile_summary=summary,
                    updated_at=datetime.utcnow(),
                )
            )
        await db.commit()
    duration = time.perf_counter() - start
    logger.debug(
        "update_student_profile_summary completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
    )
