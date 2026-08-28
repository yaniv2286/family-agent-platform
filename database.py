import time
import os
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

from loguru import logger
from sqlalchemy import Column, Integer, String, DateTime, Float, Text, ForeignKey, select, func, text, Index
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
    duration = time.perf_counter() - start
    logger.debug(
        "update_student_profile_summary completed",
        duration_seconds=round(duration, 4),
        child_name=child_name,
        subject=subject,
    )


async def add_parent_feedback(child_name: Optional[str], subject: Optional[str], message: str):
    """Store a raw parent feedback/instruction message."""
    async with TutorHistorySessionLocal() as db:
        db.add(ParentFeedback(child_name=child_name, subject=subject, message=message))
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
