from fastapi import FastAPI, BackgroundTasks, Depends, File, UploadFile, Request
import io
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from database import (
    init_db,
    get_db,
    User,
    LearningLog,
    init_tutor_history_db,
    TutorHistorySessionLocal,
    StudentProfile,
    ChatHistory,
    add_student_points,
    get_student_points,
    get_chat_by_idempotency_key,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, func
from pydantic import BaseModel
from typing import List, Optional, Dict
from datetime import datetime
from tutors import math_tutor, english_tutor, generate_speech, update_tutor_memory, transcribe_audio
from scheduler import start_scheduler, stop_scheduler
from orchestrator import run_daily_orchestration, check_telegram_feedback


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


class ChatResponse(BaseModel):
    reply: str
    points_earned: int = 0
    total_points: int = 0


class EnglishChatRequest(BaseModel):
    user_id: int
    messages: List[Dict[str, str]]
    idempotency_key: Optional[str] = None


class SpeakRequest(BaseModel):
    text: str
    subject: str = "math"


class EndSessionRequest(BaseModel):
    user_id: int
    subject: str
    messages: List[Dict[str, str]]
    duration_minutes: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize the databases
    await init_db()
    await init_tutor_history_db()
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

# Configure loguru plain-text file logging
os.makedirs("logs", exist_ok=True)
logger.remove()
logger.add(
    "logs/tutor_app_{time:YYYY-MM-DD}.log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}{exception}",
    rotation="00:00",
    retention="14 days",
    enqueue=True,
    encoding="utf-8",
)
logger.add(sys.stderr, colorize=True, level="INFO", enqueue=True)

# Application-level PIN. The default is for local development only; set
# APP_PIN in .env for any shared/local-network deployment.
APP_PIN = os.getenv("APP_PIN", "1234")


@app.middleware("http")
async def pin_middleware(request: Request, call_next):
    if (
        request.url.path.startswith("/api/")
        and request.url.path != "/api/telegram/webhook"
    ):
        if request.headers.get("x-app-pin") != APP_PIN:
            return JSONResponse(
                {"detail": "Unauthorized: missing or incorrect PIN"},
                status_code=401,
            )
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start_time = time.perf_counter()
    with logger.contextualize(request_id=request_id):
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


# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.bind(request_id=request_id).exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "Internal error occurred"},
        headers={"X-Request-ID": request_id},
    )


@app.get("/")
async def serve_index():
    """Serve the main index.html file"""
    return FileResponse("static/index.html")


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


@app.post("/api/orchestrator/run-now")
async def orchestrator_run_now():
    """
    Manually trigger the daily orchestration job (gather activity, generate
    the summary, and send it to Yaniv on Telegram) without waiting for the
    scheduled time. Useful for testing the Telegram integration.
    """
    try:
        result = await run_daily_orchestration()
        return {
            "status": "ok",
            "telegram_sent": result["telegram_sent"],
            "summary_text": result["summary_text"],
        }
    except Exception as e:
        logger.exception("Manual orchestrator run failed")
        return {"status": "error", "message": str(e)}


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
    # now returns (reply_text, points_earned).
    reply_text, points_earned = await tutor.get_llm_response(request.messages, user_profile)
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
    
    return ChatResponse(reply=reply_text, points_earned=points_earned, total_points=total_points)


@app.post("/api/tutor/english", response_model=ChatResponse)
async def tutor_english(request: EnglishChatRequest, background_tasks: BackgroundTasks):
    """
    Dedicated English-tutor chat endpoint.
    """
    user_profile = await english_tutor.get_user_profile(request.user_id)
    if not user_profile:
        return ChatResponse(reply="מצטער, לא מצאתי את הפרופיל שלך. אנא נסה שוב.")
    
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
    
    reply_text, points_earned = await english_tutor.get_llm_response(request.messages, user_profile)
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
    
    return ChatResponse(reply=reply_text, points_earned=points_earned, total_points=total_points)


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
    Convert the provided text to MP3 speech using OpenAI's TTS-1 model,
    selecting a distinct voice for math vs. English so the two tutors sound
    different, and stream it back to the client.
    """
    try:
        text = re.sub(r'([א-ת])-(\d+)', r'\1 \2', request.text)
        tts_response = await generate_speech(text, request.subject)
        return StreamingResponse(
            io.BytesIO(tts_response.content),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline; filename=speech.mp3"},
        )
    except RuntimeError:
        return {"error": "OPENAI_API_KEY is not configured."}
    except Exception as e:
        logger.exception("OpenAI TTS request failed")
        return {"error": "Failed to generate speech."}


@app.post("/api/tutor/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """
    Transcribe an uploaded audio file using OpenAI's Whisper-1 model.
    Used as a fallback for mobile browsers that do not support Web Speech API.
    """
    try:
        audio_bytes = await file.read()
        text = await transcribe_audio(audio_bytes, file.filename or "recording.mp3")
        return {"text": text}
    except RuntimeError:
        return {"error": "OPENAI_API_KEY is not configured."}
    except Exception as e:
        logger.exception("Whisper transcription request failed")
        return {"text": ""}


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


@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    expected = os.getenv("TELEGRAM_SECRET_TOKEN")
    if not expected or secret != expected:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    await check_telegram_feedback(data)
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
