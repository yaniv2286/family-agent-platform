from fastapi import FastAPI, BackgroundTasks, Depends, File, Form, UploadFile, Request, HTTPException, Body
import io
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

APP_VERSION = "1.2.0"
_PROCESS_START = time.time()


_BUILD_INFO = Path(__file__).resolve().with_name("build_info.json")
_DEPLOYED_AT_CACHE = {"mtime": None, "value": None}


def _load_deployed_at():
    """Deployed-at timestamp, re-read when build_info.json changes.

    deploy.bat rewrites build_info.json on every deploy - but a long-running
    local server used to report the timestamp it loaded at startup forever,
    so the footer showed a stale deploy time until restart.
    """
    if os.getenv("APP_DEPLOYED_AT"):
        return os.getenv("APP_DEPLOYED_AT")
    try:
        mtime = _BUILD_INFO.stat().st_mtime
    except OSError:
        return None
    if _DEPLOYED_AT_CACHE["mtime"] != mtime:
        try:
            _DEPLOYED_AT_CACHE["value"] = json.loads(
                _BUILD_INFO.read_text(encoding="utf-8")
            ).get("deployed_at")
        except (OSError, json.JSONDecodeError, AttributeError):
            _DEPLOYED_AT_CACHE["value"] = None
        _DEPLOYED_AT_CACHE["mtime"] = mtime
    return _DEPLOYED_AT_CACHE["value"]
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
import exceptions
from exceptions import AppError
from alert_router import alert
from database import (
    init_db,
    get_db,
    User,
    LearningLog,
    init_tutor_history_db,
    TutorHistorySessionLocal,
    StudentProfile,
    ChatHistory,
    TutorSkillMastery,
    TutorPracticeAttempt,
    EnglishChildWallet,
    EnglishCoursePlacement,
    EnglishLessonProgress,
    add_student_points,
    get_student_points,
    get_chat_by_idempotency_key,
    TUTOR_CURRICULUM,
    grade_level_cap,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, func
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from tutors import math_tutor, english_tutor, generate_speech, update_tutor_memory, transcribe_audio, stream_speech
from scheduler import start_scheduler, stop_scheduler
from orchestrator import router as orchestrator_router
from collaboration_api import router as collaboration_router
from control_plane import capture_agent_log, router as control_plane_router
from fitness_agent import router as fitness_router
from agents.messenger_agent import router as messenger_router
from agents.data_agent import router as data_router
from agents.nutrition_agent import router as nutrition_router
from agents.nutrition_coach_agent import router as meal_coach_router
from agents.meal_assistant import router as meal_assistant_router
from agents.scout_ideas_agent import router as scout_router
from nutrition_db import init_nutrition_db
from rbac import resolve_role, Role
from english_curriculum import (
    COURSES,
    list_courses,
    get_course,
    get_lesson,
    course_ids,
)
from lesson_engine import (
    available_lesson_ids,
    load_lesson,
    public_slide,
    grade as grade_slide,
)
from english_progress import record_lesson_completion


# Pydantic models for response
class UserResponse(BaseModel):
    model_config = {"from_attributes": True}
    
    id: int
    name: str
    role: str
    grade_level: Optional[str] = None
    interests: Optional[str] = None
    created_at: Optional[datetime] = None


class ChatRequest(BaseModel):
    user_id: int
    subject: str
    messages: List[Dict[str, str]]
    idempotency_key: Optional[str] = None
    focus_skill_id: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    points_earned: int = 0
    total_points: int = 0
    skill_id: Optional[str] = None
    answer_correct: Optional[bool] = None
    hints_used: int = 0
    gesture: Optional[str] = None


class EnglishChatRequest(BaseModel):
    user_id: int
    messages: List[Dict[str, str]]
    idempotency_key: Optional[str] = None
    focus_skill_id: Optional[str] = None


class SpeakRequest(BaseModel):
    text: str
    subject: str = "math"
    student_name: Optional[str] = None


class EndSessionRequest(BaseModel):
    user_id: int
    subject: str
    messages: List[Dict[str, str]]
    duration_minutes: int


class TutorAttemptRequest(BaseModel):
    subject: str
    skill_id: str
    correct: bool
    hints_used: int = Field(default=0, ge=0, le=10)
    difficulty: int = Field(default=1, ge=1, le=5)
    idempotency_key: str = Field(min_length=8, max_length=200)





@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Validate configuration and dependencies before opening the API.
    from scripts.startup_validator import validate
    result = await validate()
    if not result.ok:
        for error in result.errors:
            logger.error(f"Startup validation failed: {error}")
        raise RuntimeError(f"Startup validation failed: {'; '.join(result.errors)}")
    for warning in result.warnings:
        logger.warning(f"Startup validation warning: {warning}")

    # Startup: Initialize the databases.
    await init_db()
    await init_tutor_history_db()
    await init_nutrition_db()
    start_scheduler()
    yield
    # Shutdown: Cleanup if needed
    stop_scheduler()


# Create FastAPI app with lifespan
app = FastAPI(
    title="Family Agent Platform",
    description="Backend for family learning and health tracking",
    version="1.0.0",
    lifespan=lifespan
)

# Load all API routers once at import time. Router registration must not happen
# inside lifespan() because that would mutate the global app on every startup
# and create merged-lifespan recursion.
for _router in (
    fitness_router,
    collaboration_router,
    control_plane_router,
    orchestrator_router,
    messenger_router,
    data_router,
    nutrition_router,
    meal_assistant_router,
    meal_coach_router,
    scout_router,
):
    app.include_router(_router)

# Configure loguru structured logging.
os.makedirs("logs", exist_ok=True)
logger.remove()

_SERVICE = "koko"
_LOG_FORMAT = os.environ.get("LOG_FORMAT", "text").lower()

if _LOG_FORMAT == "json":
    _FILE_FORMAT = "{\"time\":\"{time:YYYY-MM-DDTHH:mm:ssZ}\",\"service\":\"" + _SERVICE + "\",\"correlation_id\":\"{extra[request_id]}\",\"level\":\"{level}\",\"name\":\"{name}\",\"function\":\"{function}\",\"line\":{line},\"message\":\"{message}\"}{exception}" 
    _STDERR_FORMAT = _FILE_FORMAT
else:
    _FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | service={extra[service]} | correlation_id={extra[request_id]} | {level: <8} | {name}:{function}:{line} | {message}{exception}"
    _STDERR_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}{exception}"

logger.configure(extra={"service": _SERVICE, "request_id": "none"})
logger.add(
    "logs/tutor_app_{time:YYYY-MM-DD}.log",
    format=_FILE_FORMAT,
    rotation="00:00",
    retention="14 days",
    enqueue=True,
    encoding="utf-8",
    level="INFO",
)
logger.add(sys.stderr, colorize=True, level="INFO", enqueue=True, format=_STDERR_FORMAT)
logger.add(capture_agent_log, level="INFO")

@app.middleware("http")
async def pin_middleware(request: Request, call_next):
    exempt = {"/api/messenger/webhook/telegram", "/api/health"}
    if (
        request.url.path.startswith("/api/")
        and request.url.path not in exempt
    ):
        pin = request.headers.get("x-app-pin")
        role = resolve_role(pin)
        if not role:
            return JSONResponse(
                {"detail": "Unauthorized: missing or incorrect PIN"},
                status_code=401,
            )
        request.state.role = role
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start_time = time.perf_counter()
    with logger.contextualize(request_id=request_id, request_path=request.url.path):
        logger.info(f"Request started: {request.method} {request.url.path}")
        response = await call_next(request)
        process_time = time.perf_counter() - start_time
        logger.info(
            f"Request completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_seconds=round(process_time, 4),
        )
    response.headers["X-Request-ID"] = request_id
    return response


# Static files are mounted here; all API routers are loaded inside lifespan()
# so startup validation and database initialization run first.
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/api/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """Public health endpoint for load balancers and Docker healthchecks.

    Verifies the main database and the tutor history database are reachable.
    """
    checks = {"main_db": False, "tutor_history_db": False}
    try:
        await db.execute(text("SELECT 1"))
        checks["main_db"] = True
    except Exception as exc:
        logger.exception("Health check failed for main database")
        checks["main_db_error"] = str(exc)

    try:
        from database import TutorHistorySessionLocal
        async with TutorHistorySessionLocal() as tutor_db:
            await tutor_db.execute(text("SELECT 1"))
        checks["tutor_history_db"] = True
    except Exception as exc:
        logger.exception("Health check failed for tutor history database")
        checks["tutor_history_db_error"] = str(exc)

    healthy = all(checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "version": APP_VERSION,
            "deployed_at": _load_deployed_at(),
            "uptime_seconds": round(time.time() - _PROCESS_START, 3),
            "checks": checks,
        },
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    request_id = getattr(request.state, "request_id", "unknown")
    level = "error" if not isinstance(exc, (exceptions.ConfigError, exceptions.SecurityError)) else "critical"
    await alert(
        level=level,
        component=exc.component,
        message=exc.message,
        error=exc,
        request_id=request_id,
        exc=exc,
    )
    status_code = 503 if isinstance(exc, (exceptions.ConfigError, exceptions.DataError, exceptions.AgentError)) else 500
    return JSONResponse(
        status_code=status_code,
        content={"status": "error", "message": exc.message},
        headers={"X-Request-ID": request_id},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.bind(request_id=request_id).exception("Unhandled exception")
    # Route critical unexpected errors to configured alert channels.
    await alert(
        level="error",
        component="unknown",
        message=f"Unhandled {type(exc).__name__}: {str(exc)[:200]}",
        request_id=request_id,
        exc=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "Internal error occurred"},
        headers={"X-Request-ID": request_id},
    )


@app.get("/")
async def serve_index():
    """Serve the main index.html file"""
    return FileResponse(
        "static/index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/dashboard")
async def serve_dashboard():
    """
    Serve the parent management dashboard. Intentionally not linked from
    the kids-facing index.html - this is a hidden route for parents only.
    """
    return FileResponse("static/dashboard.html")


@app.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    Health check endpoint to verify the API and database are working.
    """
    try:
        # Test database connection
        await db.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}


@app.get("/api/users", response_model=List[UserResponse])
async def get_users(db: AsyncSession = Depends(get_db)):
    """
    Get all registered users from the database.
    """
    result = await db.execute(select(User))
    users = result.scalars().all()
    return users


@app.get("/api/dashboard/stats")
async def dashboard_stats(db: AsyncSession = Depends(get_db)):
    """
    Aggregate per-child, per-subject learning data for the parent dashboard:
    - The AI-generated long-term profile_summary for math and english.
    - The 5 most recent chat_history messages for each subject, so parents
      can see recent activity without digging through raw logs.
    """
    result = await db.execute(select(User).where(User.role == "child"))
    children = result.scalars().all()

    subjects = ("math", "english")
    dashboard = []

    async with TutorHistorySessionLocal() as history_db:
        for child in children:
            child_name = child.name
            subjects_data = {}

            for subject in subjects:
                profile_result = await history_db.execute(
                    select(StudentProfile).where(
                        StudentProfile.child_name == child_name,
                        StudentProfile.subject == subject,
                    )
                )
                profile = profile_result.scalar_one_or_none()

                messages_result = await history_db.execute(
                    select(ChatHistory)
                    .where(ChatHistory.child_name == child_name, ChatHistory.subject == subject)
                    .order_by(ChatHistory.timestamp.desc())
                    .limit(5)
                )
                recent_messages = list(reversed(messages_result.scalars().all()))

                subjects_data[subject] = {
                    "profile_summary": profile.profile_summary if profile else None,
                    "updated_at": profile.updated_at if profile else None,
                    "recent_messages": [
                        {"role": m.role, "content": m.content, "timestamp": m.timestamp}
                        for m in recent_messages
                    ],
                }

            dashboard.append({
                "user_id": child.id,
                "child_name": child_name,
                "grade_level": child.grade_level,
                "subjects": subjects_data,
            })

    return {"children": dashboard}


@app.post("/api/tutor/chat", response_model=ChatResponse)
async def tutor_chat(request: ChatRequest, background_tasks: BackgroundTasks):
    """
    Chat with the tutor (math or english) using the real OpenAI LLM (async),
    then persist the turn and update the dynamic student profile in the background.
    """
    # Select tutor based on subject
    if request.subject == "english":
        tutor = english_tutor
    else:
        tutor = math_tutor
    
    # Get user profile
    user_profile = await tutor.get_user_profile(request.user_id)
    if not user_profile:
        return ChatResponse(reply="מצטער, לא מצאתי את הפרופיל שלך. אנא נסה שוב.")
    if request.focus_skill_id:
        curriculum = _curriculum(request.subject)
        if request.focus_skill_id in {skill["skill_id"] for skill in curriculum}:
            user_profile["focus_skill_id"] = request.focus_skill_id
        else:
            # A stale daily-practice skill from another subject must not kill
            # the turn - the child sees a 422 as Koko literally saying
            # "undefined" (production, Nov 23). Log and continue without it.
            logger.warning(
                "Ignoring unknown focus_skill_id",
                child_name=user_profile["name"], subject=request.subject,
                focus_skill_id=request.focus_skill_id,
            )

    # Idempotency: if this exact request has already been processed, replay the
    # stored response without calling the LLM or awarding points again.
    if request.idempotency_key:
        previous = await get_chat_by_idempotency_key(
            user_profile["name"], request.subject, request.idempotency_key
        )
        if previous:
            current_total = await get_student_points(user_profile["name"], request.subject)
            logger.info(
                "Idempotent replay for chat request",
                child_name=user_profile["name"],
                subject=request.subject,
                idempotency_key=request.idempotency_key,
            )
            return ChatResponse(
                reply=previous.content,
                points_earned=0,
                total_points=current_total,
            )
    
    # Always use the real OpenAI model (async, non-blocking). get_llm_response
    # returns (reply_text, points_earned, skill_id, answer_correct, hints_used, gesture).
    reply_text, points_earned, skill_id, answer_correct, hints_used, gesture = await tutor.get_llm_response(request.messages, user_profile)
    total_points = await add_student_points(user_profile["name"], request.subject, points_earned)

    # Persist the new turn and update the long-term profile summary in the background.
    background_tasks.add_task(
        update_tutor_memory,
        user_profile["name"],
        user_profile,
        request.messages,
        reply_text,
        request.subject,
        request.idempotency_key,
    )
    
    return ChatResponse(
        reply=reply_text,
        points_earned=points_earned,
        total_points=total_points,
        skill_id=skill_id,
        answer_correct=answer_correct,
        hints_used=hints_used,
        gesture=gesture,
    )


@app.post("/api/tutor/english", response_model=ChatResponse)
async def tutor_english(request: EnglishChatRequest, background_tasks: BackgroundTasks):
    """
    Dedicated English-tutor chat endpoint.
    """
    user_profile = await english_tutor.get_user_profile(request.user_id)
    if not user_profile:
        return ChatResponse(reply="מצטער, לא מצאתי את הפרופיל שלך. אנא נסה שוב.")
    if request.focus_skill_id:
        curriculum = _curriculum("english")
        if request.focus_skill_id in {skill["skill_id"] for skill in curriculum}:
            user_profile["focus_skill_id"] = request.focus_skill_id
        else:
            # A stale daily-practice skill from another subject must not kill
            # the turn - the child sees a 422 as Koko literally saying
            # "undefined" (production, Nov 23). Log and continue without it.
            logger.warning(
                "Ignoring unknown focus_skill_id",
                child_name=user_profile["name"], subject="english",
                focus_skill_id=request.focus_skill_id,
            )

    # Idempotency: if this exact request has already been processed, replay the
    # stored response without calling the LLM or awarding points again.
    if request.idempotency_key:
        previous = await get_chat_by_idempotency_key(
            user_profile["name"], "english", request.idempotency_key
        )
        if previous:
            current_total = await get_student_points(user_profile["name"], "english")
            logger.info(
                "Idempotent replay for chat request",
                child_name=user_profile["name"],
                subject="english",
                idempotency_key=request.idempotency_key,
            )
            return ChatResponse(
                reply=previous.content,
                points_earned=0,
                total_points=current_total,
            )
    
    reply_text, points_earned, skill_id, answer_correct, hints_used, gesture = await english_tutor.get_llm_response(request.messages, user_profile)
    total_points = await add_student_points(user_profile["name"], "english", points_earned)
    
    # Persist the new turn and update the English profile summary in the background.
    background_tasks.add_task(
        update_tutor_memory,
        user_profile["name"],
        user_profile,
        request.messages,
        reply_text,
        "english",
        request.idempotency_key,
    )
    
    return ChatResponse(
        reply=reply_text,
        points_earned=points_earned,
        total_points=total_points,
        skill_id=skill_id,
        answer_correct=answer_correct,
        hints_used=hints_used,
        gesture=gesture,
    )


def _curriculum(subject: str):
    curriculum = TUTOR_CURRICULUM.get(subject)
    if curriculum is None:
        raise HTTPException(422, "subject must be math or english")
    return curriculum


_grade_level_cap = grade_level_cap


def _mastery_status(mastery: float, attempts: int, independent_correct: int) -> str:
    if mastery >= 0.8 and attempts >= 3 and independent_correct >= 2:
        return "mastered"
    if attempts == 0:
        return "not_started"
    if mastery >= 0.5:
        return "developing"
    return "needs_support"


def _skill_response(skill: dict, mastery: Optional[TutorSkillMastery] = None):
    attempts = mastery.attempts if mastery else 0
    correct_attempts = mastery.correct_attempts if mastery else 0
    independent_correct = mastery.independent_correct if mastery else 0
    score = float(mastery.mastery if mastery else 0.0)
    return {
        **skill,
        "mastery": round(score, 3),
        "status": _mastery_status(score, attempts, independent_correct),
        "attempts": attempts,
        "correct_attempts": correct_attempts,
        "independent_correct": independent_correct,
        "accuracy": round(correct_attempts / attempts, 3) if attempts else 0.0,
        "independent_accuracy": round(independent_correct / attempts, 3) if attempts else 0.0,
        "current_difficulty": mastery.current_difficulty if mastery else skill["level"],
        "last_practiced_at": mastery.last_practiced_at if mastery else None,
        "next_review_at": mastery.next_review_at if mastery else None,
    }


async def _tutor_child(user_id: int, db: AsyncSession):
    user = await db.get(User, user_id)
    if not user or user.role != "child":
        raise HTTPException(404, "child not found")
    return user


class CoursePlacementRequest(BaseModel):
    course_id: str
    # Parent-set reward cycle: a gift after every N completed lessons.
    reward_every: Optional[int] = Field(default=None, ge=0, le=30)
    reward_text: Optional[str] = Field(default=None, max_length=120)


class LessonCompleteRequest(BaseModel):
    stars_earned: Optional[int] = Field(default=None, ge=0, le=30)


def _require_parent(request: Request):
    if getattr(request.state, "role", None) not in (Role.PARENT, Role.ADMIN):
        raise HTTPException(403, "only a parent can change course placement")


class LessonAnswerRequest(BaseModel):
    slide_id: str
    answer: object = None


def _slide(lesson: dict, slide_id: str) -> dict:
    slide = next((s for s in lesson["slides"] if s["id"] == slide_id), None)
    if slide is None:
        raise HTTPException(422, "unknown slide_id")
    return slide


async def _require_lesson_course(child_name: str, meta: dict) -> None:
    """A scripted lesson is startable only inside the parent's chosen course.

    When the parent has not placed the child yet, the catalog stays open so
    the parent can preview; once a course is assigned, lessons outside it are
    locked until the parent changes the placement.
    """
    async with TutorHistorySessionLocal() as history_db:
        placement = await history_db.get(EnglishCoursePlacement, child_name)
    if placement is not None and placement.course_id != meta["course_id"]:
        raise HTTPException(403, "lesson is not in the child's assigned course")


@app.get("/api/tutor/english/lesson/{lesson_id}")
async def english_lesson(lesson_id: str, user_id: int, db: AsyncSession = Depends(get_db)):
    """The slides of one lesson, with the answer keys removed."""
    user = await _tutor_child(user_id, db)
    meta = get_lesson(lesson_id)
    lesson = load_lesson(lesson_id)
    if meta is None or lesson is None:
        raise HTTPException(404, "lesson not available yet")
    await _require_lesson_course(user.name, meta)
    async with TutorHistorySessionLocal() as history_db:
        progress_row = (await history_db.execute(
            select(EnglishLessonProgress).where(
                EnglishLessonProgress.child_name == user.name,
                EnglishLessonProgress.lesson_id == lesson_id,
            )
        )).scalar_one_or_none()
        if progress_row is None:
            progress_row = EnglishLessonProgress(child_name=user.name, lesson_id=lesson_id)
            history_db.add(progress_row)
        already_completed = bool(progress_row.completed)
        progress_row.opened_at = datetime.utcnow()
        progress_row.updated_at = datetime.utcnow()
        await history_db.commit()
    return {
        "lesson_id": lesson_id,
        "title_he": lesson["title_he"],
        "intro_he": lesson.get("intro_he", ""),
        "stars_total": lesson.get("stars_total", len(lesson["slides"])),
        "slides": [public_slide(s) for s in lesson["slides"]],
        "already_completed": already_completed,
    }


@app.post("/api/tutor/english/lesson/{lesson_id}/answer")
async def english_lesson_answer(
    lesson_id: str, user_id: int, body: LessonAnswerRequest, db: AsyncSession = Depends(get_db)
):
    """Grade one slide. The browser never learns the answer before answering."""
    user = await _tutor_child(user_id, db)
    lesson = load_lesson(lesson_id)
    meta = get_lesson(lesson_id)
    if lesson is None or meta is None:
        raise HTTPException(404, "lesson not available yet")
    await _require_lesson_course(user.name, meta)
    result = grade_slide(_slide(lesson, body.slide_id), body.answer)
    result["child_name"] = user.name
    return result


@app.post("/api/tutor/english/lesson/{lesson_id}/complete")
async def english_lesson_complete(
    lesson_id: str,
    user_id: int,
    body: Optional[LessonCompleteRequest] = None,
    db: AsyncSession = Depends(get_db),
):
    """Record a finished lesson: slide stars, bonuses, and parent rewards."""
    user = await _tutor_child(user_id, db)
    lesson = load_lesson(lesson_id)
    meta = get_lesson(lesson_id)
    if lesson is None or meta is None:
        raise HTTPException(404, "lesson not available yet")
    await _require_lesson_course(user.name, meta)
    stars_total = lesson.get("stars_total", 0)
    # The browser counted the slides the child actually got right; when it
    # does not say (older clients), keep the previous max-stars behavior.
    earned = (
        stars_total
        if body is None or body.stars_earned is None
        else min(body.stars_earned, stars_total)
    )
    result = await record_lesson_completion(user.name, lesson_id, earned)
    return {"lesson_id": lesson_id, "completed": True, **result}


@app.get("/api/tutor/english/courses")
async def english_courses():
    """The full course catalog, including lessons that are not built yet."""
    return {"courses": list_courses()}


@app.get("/api/tutor/english/placement/{user_id}")
async def english_placement(user_id: int, db: AsyncSession = Depends(get_db)):
    """Which course this child is in. 'unassigned' until a parent picks one."""
    user = await _tutor_child(user_id, db)
    async with TutorHistorySessionLocal() as history_db:
        placement = await history_db.get(EnglishCoursePlacement, user.name)
        wallet = await history_db.get(EnglishChildWallet, user.name)
        progress_rows = (await history_db.execute(
            select(EnglishLessonProgress).where(
                EnglishLessonProgress.child_name == user.name,
                EnglishLessonProgress.completed == 1,
            )
        )).scalars().all()
    lesson_progress = {row.lesson_id: row.stars for row in progress_rows}
    lesson_stats = {
        row.lesson_id: {
            "stars": row.stars,
            "duration_seconds": row.duration_seconds,
            "completed_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in progress_rows
    }
    wallet_state = {
        "total_stars": (wallet.total_stars if wallet else 0),
        "reward_every": (wallet.reward_every if wallet else 0),
        "reward_text": (wallet.reward_text if wallet else ""),
        "rewards_pending": (wallet.rewards_pending if wallet else 0),
        "lessons_done": len(lesson_progress),
    }
    if placement is None:
        return {
            "user_id": user_id,
            "child_name": user.name,
            "course": None,
            "lesson_progress": lesson_progress,
            "lesson_stats": lesson_stats,
            "wallet": wallet_state,
        }
    course = get_course(placement.course_id)
    return {
        "user_id": user_id,
        "child_name": user.name,
        "course_id": placement.course_id,
        "course": course,
        "lesson_progress": lesson_progress,
        "lesson_stats": lesson_stats,
        "wallet": wallet_state,
    }


@app.put("/api/tutor/english/placement/{user_id}")
async def set_english_placement(
    user_id: int,
    body: CoursePlacementRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Parent-only: put this child in a course. Age and grade are irrelevant."""
    _require_parent(request)
    if body.course_id not in course_ids():
        raise HTTPException(422, "unknown course_id")
    user = await _tutor_child(user_id, db)
    async with TutorHistorySessionLocal() as history_db:
        placement = await history_db.get(EnglishCoursePlacement, user.name)
        if placement is None:
            placement = EnglishCoursePlacement(child_name=user.name, course_id=body.course_id)
            history_db.add(placement)
        placement.course_id = body.course_id
        placement.updated_at = datetime.utcnow()
        if body.reward_every is not None or body.reward_text is not None:
            wallet = await history_db.get(EnglishChildWallet, user.name)
            if wallet is None:
                wallet = EnglishChildWallet(child_name=user.name)
                history_db.add(wallet)
            if body.reward_every is not None:
                wallet.reward_every = body.reward_every
            if body.reward_text is not None:
                wallet.reward_text = body.reward_text
            wallet.updated_at = datetime.utcnow()
        await history_db.commit()
    return {
        "user_id": user_id,
        "child_name": user.name,
        "course_id": body.course_id,
    }


@app.post("/api/tutor/english/rewards/claim/{user_id}")
async def claim_english_rewards(
    user_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """Parent-only: mark all pending rewards as actually given to the child."""
    _require_parent(request)
    user = await _tutor_child(user_id, db)
    async with TutorHistorySessionLocal() as history_db:
        wallet = await history_db.get(EnglishChildWallet, user.name)
        claimed = 0
        if wallet is not None and wallet.rewards_pending:
            claimed = wallet.rewards_pending
            wallet.rewards_claimed = (wallet.rewards_claimed or 0) + claimed
            wallet.rewards_pending = 0
            wallet.updated_at = datetime.utcnow()
            await history_db.commit()
    return {"child_name": user.name, "claimed": claimed}


@app.get("/api/tutor/daily-practice/{user_id}")
async def daily_practice(user_id: int, subject: str, db: AsyncSession = Depends(get_db)):
    user = await _tutor_child(user_id, db)
    curriculum = _curriculum(subject)
    async with TutorHistorySessionLocal() as history_db:
        rows = (await history_db.execute(
            select(TutorSkillMastery).where(
                TutorSkillMastery.child_name == user.name,
                TutorSkillMastery.subject == subject,
            )
        )).scalars().all()
    mastery_by_skill = {row.skill_id: row for row in rows}
    grade_cap = _grade_level_cap(user.grade_level)
    eligible = [skill for skill in curriculum if skill["level"] <= grade_cap]
    ranked = sorted(
        eligible,
        key=lambda skill: (
            float(mastery_by_skill.get(skill["skill_id"]).mastery) if skill["skill_id"] in mastery_by_skill else 0.0,
            skill["level"],
            skill["skill_id"],
        ),
    )
    review = next(
        (
            skill for skill in ranked
            if skill["skill_id"] in mastery_by_skill
            and mastery_by_skill[skill["skill_id"]].next_review_at
            and mastery_by_skill[skill["skill_id"]].next_review_at <= datetime.utcnow()
        ),
        ranked[0],
    )
    remaining = [skill for skill in ranked if skill["skill_id"] != review["skill_id"]]
    practice = remaining[0] if remaining else review
    challenge = next(
        (skill for skill in curriculum if skill["skill_id"] not in {review["skill_id"], practice["skill_id"]} and skill["level"] >= grade_cap),
        practice,
    )
    activities = []
    for purpose, skill in (("review", review), ("practice", practice), ("challenge", challenge)):
        activities.append({"purpose": purpose, **_skill_response(skill, mastery_by_skill.get(skill["skill_id"]))})
    return {
        "child_id": user.id,
        "child_name": user.name,
        "subject": subject,
        "grade_level": user.grade_level,
        "curriculum_level": grade_cap,
        "activities": activities,
        "estimated_minutes": 20,
    }


@app.post("/api/tutor/mastery/{user_id}/attempt")
async def record_tutor_attempt(user_id: int, data: TutorAttemptRequest, db: AsyncSession = Depends(get_db)):
    user = await _tutor_child(user_id, db)
    curriculum = _curriculum(data.subject)
    skill = next((item for item in curriculum if item["skill_id"] == data.skill_id), None)
    if skill is None:
        raise HTTPException(422, "unknown skill_id for subject")

    async with TutorHistorySessionLocal() as history_db:
        previous = (await history_db.execute(
            select(TutorPracticeAttempt).where(
                TutorPracticeAttempt.child_name == user.name,
                TutorPracticeAttempt.subject == data.subject,
                TutorPracticeAttempt.idempotency_key == data.idempotency_key,
            )
        )).scalar_one_or_none()
        mastery = (await history_db.execute(
            select(TutorSkillMastery).where(
                TutorSkillMastery.child_name == user.name,
                TutorSkillMastery.subject == data.subject,
                TutorSkillMastery.skill_id == data.skill_id,
            )
        )).scalar_one_or_none()
        if previous:
            response = _skill_response(skill, mastery)
            response["idempotent_replay"] = True
            return response

        now = datetime.utcnow()
        if mastery is None:
            mastery = TutorSkillMastery(
                child_name=user.name,
                subject=data.subject,
                skill_id=data.skill_id,
                current_difficulty=data.difficulty,
            )
            history_db.add(mastery)
        assisted = data.hints_used > 0
        if data.correct:
            delta = 0.1 if assisted else 0.18
        else:
            delta = -0.08
        mastery.mastery = max(0.0, min(1.0, float(mastery.mastery or 0.0) + delta))
        mastery.attempts = int(mastery.attempts or 0) + 1
        mastery.correct_attempts = int(mastery.correct_attempts or 0) + int(data.correct)
        mastery.independent_correct = int(mastery.independent_correct or 0) + int(data.correct and not assisted)
        mastery.current_difficulty = data.difficulty
        mastery.last_practiced_at = now
        review_days = 1 if not data.correct else 7 if mastery.mastery >= 0.8 else 2
        mastery.next_review_at = now + timedelta(days=review_days)
        history_db.add(TutorPracticeAttempt(
            child_name=user.name,
            subject=data.subject,
            skill_id=data.skill_id,
            correct=int(data.correct),
            hints_used=data.hints_used,
            difficulty=data.difficulty,
            idempotency_key=data.idempotency_key,
        ))
        response = _skill_response(skill, mastery)
        response["idempotent_replay"] = False
        await history_db.commit()
        return response


@app.get("/api/tutor/progress/{user_id}")
async def tutor_progress(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await _tutor_child(user_id, db)
    async with TutorHistorySessionLocal() as history_db:
        rows = (await history_db.execute(
            select(TutorSkillMastery).where(TutorSkillMastery.child_name == user.name)
        )).scalars().all()
    mastery_by_key = {(row.subject, row.skill_id): row for row in rows}
    subjects = {}
    for subject, curriculum in TUTOR_CURRICULUM.items():
        skills = [
            _skill_response(skill, mastery_by_key.get((subject, skill["skill_id"])))
            for skill in curriculum
        ]
        practiced = [skill for skill in skills if skill["attempts"] > 0]
        subjects[subject] = {
            "skills": skills,
            "practiced_skills": len(practiced),
            "mastered_skills": sum(1 for skill in skills if skill["status"] == "mastered"),
            "average_mastery": round(sum(skill["mastery"] for skill in practiced) / len(practiced), 3) if practiced else 0.0,
            "total_attempts": sum(skill["attempts"] for skill in skills),
        }
    return {"child_id": user.id, "child_name": user.name, "subjects": subjects}


@app.get("/api/ping")
async def ping():
    """Lightweight authenticated health check for the frontend to validate
    a stored PIN before revealing the app UI.
    """
    return {"status": "ok"}


@app.get("/api/history/{child_name}")
async def get_history(
    child_name: str,
    subject: Optional[str] = None,
    date: Optional[str] = None,
):
    """Return a child's chat history, optionally filtered by subject and date.
    """
    async with TutorHistorySessionLocal() as db:
        query = select(ChatHistory).where(ChatHistory.child_name == child_name)
        if subject:
            query = query.where(ChatHistory.subject == subject)
        if date:
            query = query.where(func.date(ChatHistory.timestamp) == date)
        result = await db.execute(query.order_by(ChatHistory.timestamp.asc()))
        rows = result.scalars().all()
        return {
            "messages": [
                {
                    "role": r.role,
                    "content": r.content,
                    "subject": r.subject,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                }
                for r in rows
            ]
        }


@app.post("/api/tutor/speech")
async def speech(request: SpeakRequest):
    """
    Convert the provided text to MP3 speech using the configured TTS provider
    (ElevenLabs with a single shared voice, OpenAI tts-1 as fallback) and
    stream it back to the client chunk-by-chunk so playback starts on the
    first chunk instead of waiting for the whole utterance.
    """
    try:
        text = re.sub(r'([א-ת])-(\d+)', r'\1 \2', request.text)
        audio_stream = await stream_speech(text, request.subject, request.student_name)
        return StreamingResponse(
            audio_stream,
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline; filename=speech.mp3"},
        )
    except RuntimeError:
        return JSONResponse({"error": "TTS is not configured."}, status_code=503)
    except Exception as e:
        logger.exception("TTS request failed")
        return JSONResponse({"error": "Failed to generate speech."}, status_code=502)


@app.post("/api/tutor/transcribe")
async def transcribe(file: UploadFile = File(...), subject: str = Form("math")):
    """
    Transcribe an uploaded audio file using OpenAI's Whisper-1 model.
    Used as a fallback for mobile browsers that do not support Web Speech API.
    The subject selects the transcription language (English lessons must not
    force Hebrew output or a child's English comes out as Hebrew gibberish).
    """
    try:
        audio_bytes = await file.read()
        text = await transcribe_audio(audio_bytes, file.filename or "recording.mp3", subject=subject)
        return {"text": text}
    except RuntimeError:
        return {"error": "OPENAI_API_KEY is not configured."}
    except Exception as e:
        logger.exception("Whisper transcription request failed")
        return {"text": ""}


@app.post("/api/tutor/client-log")
async def client_log(payload: dict = Body(...)):
    """Client-side diagnostics sink (voice capture failures etc.) so device
    bugs are visible in the server log instead of silently failing on a
    kid's browser."""
    kind = str(payload.get("kind", "misc"))[:40]
    detail = str(payload.get("detail", ""))[:400]
    ua = str(payload.get("ua", ""))[:200]
    logger.warning("client-log kind={} detail={} ua={}", kind, detail, ua)
    return {"ok": True}


@app.post("/api/tutor/end-session")
async def end_session(request: EndSessionRequest, db: AsyncSession = Depends(get_db)):
    """
    End a tutoring session, analyze the conversation, and save to LearningLog.
    Also extracts and updates user profile information if discovered.
    """
    # Select tutor based on subject
    if request.subject == "english":
        tutor = english_tutor
    else:
        tutor = math_tutor
    
    try:
        # Analyze the session
        analysis = tutor.analyze_session(request.messages, request.subject)
        
        # Extract profile information from conversation
        profile_info = tutor.extract_profile_info(request.messages)
        
        learning_log = LearningLog(
            user_id=request.user_id,
            subject=request.subject,
            topic=analysis["topic"],
            score_delta=analysis["score_delta"],
            mistakes_summary=analysis["mistakes_summary"],
            session_duration_minutes=request.duration_minutes,
            session_date=datetime.utcnow()
        )
        db.add(learning_log)
        
        # Update user profile if new information was discovered
        result = await db.execute(select(User).where(User.id == request.user_id))
        user = result.scalar_one_or_none()
        profile_updated = False
        
        if user:
            if profile_info["grade_level"] and (not user.grade_level or user.grade_level in [None, "", "Unknown"]):
                user.grade_level = profile_info["grade_level"]
                profile_updated = True
            
            if profile_info["interests"] and (not user.interests or user.interests in [None, "", "Unknown"]):
                user.interests = profile_info["interests"]
                profile_updated = True
        
        await db.commit()
        await db.refresh(learning_log)
        
        response = {
            "id": learning_log.id,
            "user_id": learning_log.user_id,
            "subject": learning_log.subject,
            "topic": learning_log.topic,
            "score_delta": learning_log.score_delta,
            "mistakes_summary": learning_log.mistakes_summary,
            "session_duration_minutes": learning_log.session_duration_minutes,
            "session_date": learning_log.session_date,
            "profile_updated": profile_updated
        }
        
        if profile_updated:
            response["new_grade_level"] = profile_info["grade_level"]
            response["new_interests"] = profile_info["interests"]
        
        return response
    except Exception as e:
        await db.rollback()
        logger.bind(error=str(e)).exception("End session failed")
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
