from fastapi import FastAPI, BackgroundTasks, Depends, File, UploadFile, Request
import io
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from database import init_db, get_db, User, LearningLog, init_tutor_history_db
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select
from pydantic import BaseModel
from typing import List, Optional, Dict
from datetime import datetime
from tutors import math_tutor, english_tutor, generate_speech, update_tutor_memory, transcribe_audio


# Pydantic models for response
class UserResponse(BaseModel):
    model_config = {"from_attributes": True}
    
    id: int
    name: str
    role: str
    grade_level: Optional[str] = None
    interests: Optional[str] = None
    created_at: datetime


class ChatRequest(BaseModel):
    user_id: int
    subject: str
    messages: List[Dict[str, str]]


class ChatResponse(BaseModel):
    reply: str


class EnglishChatRequest(BaseModel):
    user_id: int
    messages: List[Dict[str, str]]


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
    yield
    # Shutdown: Cleanup if needed
    pass


# Create FastAPI app with lifespan
app = FastAPI(
    title="Family Agent Platform",
    description="Backend for family learning and health tracking",
    version="1.0.0",
    lifespan=lifespan
)

# Configure loguru JSON file logging
os.makedirs("logs", exist_ok=True)
logger.remove()
logger.add(
    "logs/tutor_app_{time:YYYY-MM-DD}.log",
    serialize=True,
    rotation="00:00",
    retention="14 days",
    enqueue=True,
)
logger.add(sys.stderr, colorize=True, level="INFO", enqueue=True)


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
    
    # Always use the real OpenAI model (async, non-blocking). get_llm_response
    # internally returns a clear Hebrew error message if OPENAI_API_KEY is
    # missing or the API call fails - there is no local mock engine anymore.
    reply = await tutor.get_llm_response(request.messages, user_profile)
    
    # Persist the new turn and update the long-term profile summary in the background.
    background_tasks.add_task(
        update_tutor_memory,
        user_profile["name"],
        user_profile,
        request.messages,
        reply,
        request.subject,
    )
    
    return ChatResponse(reply=reply)


@app.post("/api/tutor/english", response_model=ChatResponse)
async def tutor_english(request: EnglishChatRequest, background_tasks: BackgroundTasks):
    """
    Dedicated English-tutor chat endpoint.
    """
    user_profile = await english_tutor.get_user_profile(request.user_id)
    if not user_profile:
        return ChatResponse(reply="מצטער, לא מצאתי את הפרופיל שלך. אנא נסה שוב.")
    
    reply = await english_tutor.get_llm_response(request.messages, user_profile)
    
    # Persist the new turn and update the English profile summary in the background.
    background_tasks.add_task(
        update_tutor_memory,
        user_profile["name"],
        user_profile,
        request.messages,
        reply,
        "english",
    )
    
    return ChatResponse(reply=reply)


@app.post("/api/tutor/speech")
async def speech(request: SpeakRequest):
    """
    Convert the provided text to MP3 speech using OpenAI's TTS-1 model,
    selecting a distinct voice for math vs. English so the two tutors sound
    different, and stream it back to the client.
    """
    try:
        tts_response = await generate_speech(request.text, request.subject)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
