from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from database import init_db, get_db, User, LearningLog
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
from typing import List, Optional, Dict
from datetime import datetime
import os
from tutors import math_tutor, english_tutor


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


class EndSessionRequest(BaseModel):
    user_id: int
    subject: str
    messages: List[Dict[str, str]]
    duration_minutes: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize the database
    init_db()
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

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_index():
    """Serve the main index.html file"""
    return FileResponse("static/index.html")


@app.get("/health")
async def health_check():
    """
    Health check endpoint to verify the API and database are working.
    """
    try:
        # Test database connection
        db_gen = get_db()
        db = next(db_gen)
        db.execute(text("SELECT 1"))
        db.close()
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}


@app.get("/api/users", response_model=List[UserResponse])
async def get_users():
    """
    Get all registered users from the database.
    """
    db_gen = get_db()
    db = next(db_gen)
    try:
        users = db.query(User).all()
        return users
    finally:
        db.close()


@app.post("/api/tutor/chat", response_model=ChatResponse)
async def tutor_chat(request: ChatRequest):
    """
    Chat with the tutor (math or english). Uses LLM if configured, otherwise uses mock responses.
    """
    # Select tutor based on subject
    if request.subject == "english":
        tutor = english_tutor
    else:
        tutor = math_tutor
    
    # Get user profile
    user_profile = tutor.get_user_profile(request.user_id)
    if not user_profile:
        return ChatResponse(reply="מצטער, לא מצאתי את הפרופיל שלך. אנא נסה שוב.")
    
    # Check if a real OpenAI API key is configured
    openai_api_key = os.getenv("OPENAI_API_KEY")
    
    if openai_api_key and openai_api_key != "your_openai_api_key_here":
        # Use the real OpenAI gpt-4o-mini model (falls back to mock internally on error)
        reply = tutor.get_llm_response(request.messages, user_profile)
    else:
        # No API key configured - use the local mock response engine
        user_input = request.messages[-1].get("content", "") if request.messages else ""
        reply = tutor.get_mock_response(user_input, request.messages, user_profile)
    
    return ChatResponse(reply=reply)


@app.post("/api/tutor/end-session")
async def end_session(request: EndSessionRequest):
    """
    End a tutoring session, analyze the conversation, and save to LearningLog.
    Also extracts and updates user profile information if discovered.
    """
    # Select tutor based on subject
    if request.subject == "english":
        tutor = english_tutor
    else:
        tutor = math_tutor
    
    # Analyze the session
    analysis = tutor.analyze_session(request.messages, request.subject)
    
    # Extract profile information from conversation
    profile_info = tutor.extract_profile_info(request.messages)
    
    # Save to database
    db_gen = get_db()
    db = next(db_gen)
    try:
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
        user = db.query(User).filter(User.id == request.user_id).first()
        profile_updated = False
        
        if user:
            if profile_info["grade_level"] and (not user.grade_level or user.grade_level in [None, "", "Unknown"]):
                user.grade_level = profile_info["grade_level"]
                profile_updated = True
            
            if profile_info["interests"] and (not user.interests or user.interests in [None, "", "Unknown"]):
                user.interests = profile_info["interests"]
                profile_updated = True
        
        db.commit()
        db.refresh(learning_log)
        
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
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
