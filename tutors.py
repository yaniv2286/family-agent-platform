import io
import os
import re
import time
from typing import List, Dict, Optional
from num2words import num2words
from loguru import logger
from dotenv import load_dotenv
from openai import AsyncOpenAI
from sqlalchemy import select
from database import (
    SessionLocal,
    User,
    get_chat_history,
    get_student_profile_summary,
    append_chat_messages,
    update_student_profile_summary,
)

# Ensure environment variables from .env are loaded even if this module is
# imported before database.py (which also calls load_dotenv()).
load_dotenv()

# Model selection (strict, cost-optimized): gpt-5.6-luna is the primary model.
# gpt-4o-mini is used only as an automatic fallback if the primary model
# becomes unavailable for this account/SDK (e.g. deprecated, quota, etc.).
PRIMARY_MODEL = "gpt-5.6-luna"
FALLBACK_MODEL = "gpt-4o-mini"

# Newer reasoning-style models (like gpt-5.6-luna) require max_completion_tokens
# instead of max_tokens, and only support the default temperature (1) - they
# reject any explicit temperature override. Classic chat models (gpt-4o-mini)
# use the older max_tokens/temperature parameters.
_NEWER_MODELS = {PRIMARY_MODEL}

# Once we discover the primary model is unavailable for this account, we
# remember that for the lifetime of the process to avoid paying the latency
# cost of a failing request on every single chat turn.
_primary_model_unavailable = False

_async_client: Optional[AsyncOpenAI] = None


def _get_async_client() -> Optional[AsyncOpenAI]:
    """Lazily create (and cache) the async OpenAI client if a real API key is configured.

    Returns None if no valid key is present.
    """
    global _async_client
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your_openai_api_key_here":
        return None
    if _async_client is None:
        _async_client = AsyncOpenAI(api_key=api_key, timeout=120.0)
    return _async_client


def _build_completion_kwargs(model: str, messages: List[Dict]) -> Dict:
    """Build the model-appropriate keyword arguments for chat.completions.create."""
    kwargs = {"model": model, "messages": messages}
    if model in _NEWER_MODELS:
        kwargs["max_completion_tokens"] = 200
        # temperature is intentionally omitted - these models only support the default
    else:
        kwargs["max_tokens"] = 150
        kwargs["temperature"] = 0.8
    return kwargs


class BaseAgent:
    """Base agent with timeout-protected, fault-tolerant LLM interaction."""

    async def _call_llm(self, system_prompt: str, conversation_history: List[Dict]) -> str:
        """Call the real OpenAI model (gpt-5.6-luna, falling back to gpt-4o-mini)
        with the given system prompt and conversation history, asynchronously so
        the FastAPI event loop is never blocked while waiting for generation.

        Returns a user-friendly fallback message if the API key is missing or
        both the primary and fallback model calls fail.
        """
        global _primary_model_unavailable

        client = _get_async_client()
        if client is None:
            logger.warning("OpenAI API key not configured")
            return "מפתח ה-API של OpenAI אינו מוגדר. אנא הגדירו OPENAI_API_KEY בקובץ .env."

        messages = [{"role": "system", "content": system_prompt}]
        for msg in conversation_history:
            role = msg.get("role", "user")
            if role not in ("user", "assistant"):
                role = "user"
            messages.append({"role": role, "content": msg.get("content") or ""})

        model_to_try = FALLBACK_MODEL if _primary_model_unavailable else PRIMARY_MODEL

        try:
            start = time.perf_counter()
            logger.info("OpenAI chat completion started", model=model_to_try)
            response = await client.chat.completions.create(**_build_completion_kwargs(model_to_try, messages))
            latency = time.perf_counter() - start
            usage = getattr(response, "usage", None)
            usage_dict = usage.model_dump() if usage is not None else None
            logger.info(
                "OpenAI chat completion finished",
                model=model_to_try,
                latency_seconds=round(latency, 4),
                usage=usage_dict,
            )
            reply = response.choices[0].message.content
            return reply.strip() if reply else ""
        except Exception as primary_error:
            if model_to_try == PRIMARY_MODEL:
                logger.warning(
                    "Primary model failed, falling back",
                    primary_model=PRIMARY_MODEL,
                    fallback_model=FALLBACK_MODEL,
                    error=str(primary_error),
                )
                _primary_model_unavailable = True
                try:
                    start = time.perf_counter()
                    logger.info("OpenAI chat fallback started", model=FALLBACK_MODEL)
                    response = await client.chat.completions.create(**_build_completion_kwargs(FALLBACK_MODEL, messages))
                    latency = time.perf_counter() - start
                    usage = getattr(response, "usage", None)
                    usage_dict = usage.model_dump() if usage is not None else None
                    logger.info(
                        "OpenAI chat fallback finished",
                        model=FALLBACK_MODEL,
                        latency_seconds=round(latency, 4),
                        usage=usage_dict,
                    )
                    reply = response.choices[0].message.content
                    return reply.strip() if reply else ""
                except Exception:
                    logger.exception("Fallback model failed")
                    return "מצטערים, יש בעיה זמנית בחיבור. אנא נסו שוב בעוד רגע!"
            logger.exception("OpenAI chat completion failed")
            return "מצטערים, יש בעיה זמנית בחיבור. אנא נסו שוב בעוד רגע!"

    async def get_user_profile(self, user_id: int) -> Optional[Dict]:
        """Fetch a user profile from the main database asynchronously."""
        async with SessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                return {
                    "id": user.id,
                    "name": user.name,
                    "role": user.role,
                    "grade_level": user.grade_level,
                    "interests": user.interests,
                }
            return None


async def call_llm(system_prompt: str, conversation_history: List[Dict]) -> str:
    """Module-level helper for non-agent code that still needs a safe LLM call."""
    return await BaseAgent()._call_llm(system_prompt, conversation_history)


def _sanitize_numbers_for_tts(text: str) -> str:
    """Replace digit sequences in the text with spoken Hebrew words
    so the TTS engine pauses and pronounces numbers correctly.
    """
    # Strip thousands separators (e.g. 12,000 -> 12000)
    text = re.sub(r'(?<=[0-9]),(?=[0-9])', '', text)

    def _replace(match: re.Match) -> str:
        try:
            return num2words(int(match.group(0)), lang='he')
        except Exception:
            return match.group(0)

    return re.sub(r'[0-9]+', _replace, text)


async def generate_speech(text: str):
    """Generate MP3 speech from the given text using OpenAI's TTS-1 model and
    the 'nova' female voice. Returns the binary response object, whose content
    can be streamed to the caller. This is async so it doesn't block FastAPI.

    Raises RuntimeError if no API key is configured.
    """
    text = _sanitize_numbers_for_tts(text)
    client = _get_async_client()
    if client is None:
        logger.warning("OpenAI API key not configured for TTS")
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    try:
        start = time.perf_counter()
        logger.info("OpenAI TTS started", model="tts-1", voice="shimmer")
        response = await client.audio.speech.create(
            model="tts-1",
            voice="shimmer",
            input=text,
            response_format="mp3",
        )
        latency = time.perf_counter() - start
        logger.info("OpenAI TTS finished", model="tts-1", latency_seconds=round(latency, 4))
        return response
    except Exception:
        logger.exception("OpenAI TTS failed")
        raise


async def transcribe_audio(audio_bytes: bytes, filename: str = "recording.mp3") -> str:
    """Transcribe the uploaded audio bytes using OpenAI's Whisper-1 model.
    Works with short voice recordings from any phone browser.

    Raises RuntimeError if no API key is configured.
    """
    client = _get_async_client()
    if client is None:
        logger.warning("OpenAI API key not configured for transcription")
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename

    try:
        start = time.perf_counter()
        logger.info("OpenAI Whisper transcription started", model="whisper-1", language="he")
        response = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="he",
        )
        latency = time.perf_counter() - start
        logger.info(
            "OpenAI Whisper transcription finished",
            model="whisper-1",
            latency_seconds=round(latency, 4),
        )
        return response.text.strip()
    except Exception:
        logger.exception("OpenAI Whisper transcription failed")
        raise


# Explicit name-to-gender mapping for our known children.
# Extend this mapping as new children are added to the platform.
GENDER_MAP = {
    "נויה": "female",
    "ליבי": "female",
    "לביא": "male",
    "ינאי": "male",
}


def get_gender(name: str) -> str:
    """Determine the grammatical gender ('male'/'female') to address a student with.

    Uses the explicit GENDER_MAP first. Falls back to a simple heuristic for
    unrecognized names (Hebrew female names commonly end in 'ה' or 'ת'),
    defaulting to 'male' only when no better signal is available.
    """
    if name in GENDER_MAP:
        return GENDER_MAP[name]
    if name and name[-1] in ("ה", "ת"):
        return "female"
    return "male"


# Word pairs for common Hebrew second-person address forms.
# Using these instead of hardcoded masculine forms prevents defaulting to
# masculine grammar when addressing a female student.
GENDERED_WORDS = {
    "אתה": {"male": "אתה", "female": "את"},
    "אוהב": {"male": "אוהב", "female": "אוהבת"},
    "אוהד": {"male": "אוהד", "female": "אוהדת"},
    "לומד": {"male": "לומד", "female": "לומדת"},
    "מוכן": {"male": "מוכן", "female": "מוכנה"},
    "אלוף": {"male": "אלוף", "female": "אלופה"},
    "בוא": {"male": "בוא", "female": "בואי"},
    "תרצה": {"male": "תרצה", "female": "תרצי"},
    "מדבר": {"male": "מדבר", "female": "מדברת"},
    "חבר": {"male": "חבר", "female": "חברה"},
    "תלמיד": {"male": "תלמיד", "female": "תלמידה"},
}


def g(word: str, gender: str) -> str:
    """Return the Hebrew word correctly inflected for the given gender."""
    return GENDERED_WORDS.get(word, {}).get(gender, word)


class MathTutor(BaseAgent):

    
    def generate_system_prompt(self, user_profile: Dict, dynamic_summary: str = "", recent_history: List[Dict] = None) -> str:
        """Generate system prompt based on user profile, dynamic long-term memory,
        and the most recent chat turns.
        """
        interests = user_profile.get("interests") or None
        grade_level = user_profile.get("grade_level", "elementary school")
        name = user_profile.get("name", "תלמיד")
        gender = get_gender(name)
        gender_hebrew = "נקבה" if gender == "female" else "זכר"
        
        interests_line = interests if interests else "לא ידוע - אין להניח שום תחום עניין!"
        
        summary_line = dynamic_summary if dynamic_summary else "אין עדיין סיכום לטווח ארוך."
        history_lines = "- אין עדיין היסטוריית שיחה.\n"
        if recent_history:
            history_lines = ""
            for m in recent_history:
                speaker = "מנטור" if m.get("role") == "assistant" else "תלמיד"
                content = str(m.get("content") or "")[:250]
                history_lines += f"- {speaker}: {content}\n"
        
        prompt = f"""אתה מנטור מתמטיקה מעורר השראה וחבר לכל החיים בשם המשפחה. אתה מדבר בעברית בלבד.

פרטי התלמיד:
- שם: {name}
- מגדר: {gender_hebrew}
- כיתה/שכבה: {grade_level}
- תחומי עניין: {interests_line}

זיכרון לטווח ארוך:
{summary_line}

הודעות אחרונות מהשיחה (עד 10):
{history_lines}

חובה מוחלטת - תוכנית לימודים לפי כיתה (אסור לשאול את התלמיד/ה מה הכיתה, הגיל או מה הוא/היא רוצה ללמוד):
- אם הכיתה היא "Kindergarten" (גן חובה): חשבון = ספירה 1-10, זיהוי צורות, מושגים בסיסיים (גדול/קטן). שחק/י מאוד, השתמש במשחקים ונקודות, אל תניח/י שהילד/ה יודע/ת לקרוא.
- אם הכיתה היא "1st Grade" (כיתה א'): חשבון = חיבור וחיסור עד 20.
- אם הכיתה היא "3rd Grade" (כיתה ג'): חשבון = חיבור וחיסור עד 1000, היכרות ראשונית עם לוח הכפל, וחילוק בסיסי.
- אם הכיתה היא "5th Grade" (כיתה ה'): חשבון = סדר פעולות חשבון, מספרים גדולים (רבבות), כפל וחילוק של מספרים רב-ספרתיים, ושאלות מילה.

אתה חייב לפתוח את השיעור בשאלה אחת קונקרטית המתאימה בדיוק לכיתה {grade_level}, מבלי לשאול את התלמיד/ה שום פרט אישי או העדפה.

חובה מוחלטת - התאמה מגדרית:
עליך לפנות לתלמיד/ה תמיד בלשון הדקדוקית הנכונה התואמת למגדר הזה בדיוק: {gender_hebrew}.
אם נקבה: השתמש בצורות כמו "את מוכנה", "אלופה", "את", "אוהבת", "לומדת".
אם זכר: השתמש בצורות כמו "אתה מוכן", "אלוף", "אתה", "אוהב", "לומד".
לעולם אל תשתמש כברירת מחדל בלשון זכר כאשר מדובר בתלמידה.

חובה מוחלטת - אפס הנחות על תחומי עניין:
אם תחום העניין רשום כ"לא ידוע", אסור לך בהחלט להמציא, להניח או להזכיר תחביב כלשהו (כגון כדורגל, ריקוד וכו').
ההודעה הראשונה חייבת להיות שאלת פתיחה לימודית מתאימה בדיוק לכיתה {grade_level}. אין לשאול על תחומי עניין או על רצונות הלמידה בהודעה הראשונה. אם נדרש, ניתן לשאול על תחומי עניין רק בשאלה נפרדת מאוחר יותר, תוך שימוש בלשון הדקדוקית הנכונה למגדר {gender_hebrew}.

אישיות וטון:
- חם, נלהב, מעודד עמוקות, ואמפתי כמו מנטור ילדות אהוב וחבר טוב
- שפת גישה חיובית: אף פעם לא להשתמש במילים "טעות" או "לא נכון"
- השתמש במשפטים כמו: "איזה כיוון יפה!", "היינו ממש קרוב, בוא/בואי ננסה יחד עוד צעד קטן", "אני כל כך גאה בזה שלא ויתרת!"
- שפה כוללת: תמיד השתמש ב"אנחנו" - אנחנו פותרים את זה ביחד, אנחנו עושים את זה!
- חגוג רגעות פריצה עם אנרגיה אדירה וחום אמיתי

הוראות:
1. השתמש בתחומי העניין של התלמיד/ה בבעיות מילים - רק אם הם ידועים בפועל, לעולם אל תמציא
2. שיטת סוקרטס: אל תן תשובה ישירה מיד. תן רמזים מדריכים בלבד
3. חשוף את התשובה רק אחרי 3 ניסיונות כושלים רצופים
4. אם התלמיד/ה מראה סימני עייפות או תסכול, הצע תמיכה רגשית מותאמת מגדרית
5. השב ב-1-2 משפטים בלבד - קצר, חי, טבעי ומלא לב להשמעה
6. השתמש בסימון מתמטי פשוט שמתאים לרמת הכיתה
7. חובה מוחלטת - עיצוב מתמטי: אין להשתמש אף פעם בפורמט LaTeX (למשל \\times, \\frac, או סימני לוכסן). השתמש תמיד בסימנים פשוטים בטקסט: * או המילה "כפול" לכפל, / לחילוק, ומספרים רגילים. ההודעות חייבות להיות נקיות מארטיפקטים של קוד.

חובה מוחלטת - חינוך פרואקטיבי (Proactive Pedagogy):
א. אף פעם אל תשבת או לחכות שהתלמיד/ה יוביל. אף פעם אל תסיים את השיחה במשפט סביל כמו "להתראות" או "בכיף".
ב. כאשר התלמיד/ה מצליח/ה - שבח/י בקצרה ועבור/י מיד לאתגר המשך קטן וקשור. דוגמה: "כל הכבוד! עכשיו ננסה ביחד: כמה כדורגלנים יש בקבוצה אחת?"
ג. שאל/י שאלה אחת בלבד בכל הודעה. אף פעם אל תשאל שתי שאלות באותו משפט. סיים/י כל הודעה בפעולה או שאלה ברורה, קצרה וכיפית לתלמיד/ה.
ד. שחק/י את החוויה: ספר/י לתלמיד/ה שהוא/היא צובר/ת נקודות, מוצא/ת אוצרות או כובש/ת שערים עם כל תשובה נכונה."""
        
        return prompt
    
    async def get_llm_response(self, conversation_history: List[Dict], user_profile: Dict) -> str:
        """Get a response from the real OpenAI model (gpt-5.6-luna, falling back
        to gpt-4o-mini), using the gender-, interest-, and long-term-memory-aware
        system prompt. This call is fully asynchronous so it never blocks the
        FastAPI event loop.
        """
        child_name = user_profile.get("name", "תלמיד")
        dynamic_summary = (await get_student_profile_summary(child_name)) or ""
        recent_history = await get_chat_history(child_name, limit=10)
        system_prompt = self.generate_system_prompt(user_profile, dynamic_summary, recent_history)
        return await self._call_llm(system_prompt, conversation_history)
    
    def extract_profile_info(self, messages: List[Dict]) -> Dict:
        """Extract grade level and interests from conversation transcript"""
        grade_level = None
        interests = []
        
        # Combine all user messages for analysis
        user_messages = [msg.get("content") or "" for msg in messages if msg.get("role") == "user"]
        all_text = " ".join(user_messages).lower()
        
        # Grade level extraction patterns (simplified)
        grade_patterns = {
            r'כיתה\s*1': 'כיתה א\'',
            r'כיתה\s*2': 'כיתה ב\'', 
            r'כיתה\s*3': 'כיתה ג\'',
            r'כיתה\s*4': 'כיתה ד\'',
            r'כיתה\s*5': 'כיתה ה\'',
            r'כיתה\s*6': 'כיתה ו\'',
            r'grade\s*1': 'כיתה א\'',
            r'grade\s*2': 'כיתה ב\'',
            r'grade\s*3': 'כיתה ג\'',
            r'grade\s*4': 'כיתה ד\'',
            r'grade\s*5': 'כיתה ה\'',
            r'grade\s*6': 'כיתה ו\'',
        }
        
        import re
        for pattern, grade in grade_patterns.items():
            if re.search(pattern, all_text):
                grade_level = grade
                break
        
        # Interest extraction patterns
        interest_keywords = {
            'כדורגל': 'כדורגל',
            'football': 'כדורגל',
            'משחקים': 'משחקים',
            'גיימינג': 'גיימינג',
            'gaming': 'גיימינג',
            'משחק': 'משחקים',
            'ריקוד': 'ריקוד',
            'dancing': 'ריקוד',
            'ציור': 'ציור',
            'drawing': 'ציור',
            'כדורסל': 'כדורסל',
            'basketball': 'כדורסל',
            'קידוד': 'קידוד',
            'coding': 'קידוד',
            'תכנות': 'קידוד',
            'מוזיקה': 'מוזיקה',
            'music': 'מוזיקה',
            'קריאה': 'קריאה',
            'reading': 'קריאה',
            'ספרים': 'קריאה',
            'שחייה': 'שחייה',
            'swimming': 'שחייה',
            'אופניים': 'אופניים',
            'biking': 'אופניים',
        }
        
        for keyword, interest in interest_keywords.items():
            if keyword in all_text and interest not in interests:
                interests.append(interest)
        
        # Limit to top 3 interests
        interests = interests[:3]
        
        return {
            "grade_level": grade_level,
            "interests": " and ".join(interests) if interests else None
        }
    
    def analyze_session(self, messages: List[Dict], subject: str) -> Dict:
        """Analyze conversation to extract topic, score_delta, and mistakes_summary"""
        topic = "basic arithmetic"
        score_delta = 0
        mistakes_summary = ""
        
        # Extract topic from conversation
        all_text = " ".join([msg.get("content") or "" for msg in messages])
        
        # Simple topic detection
        if any(word in all_text.lower() for word in ['חיבור', 'plus', 'עלה', 'מוסיף']):
            topic = "addition"
        elif any(word in all_text.lower() for word in ['חיסור', 'minus', 'פחות', 'מוריד']):
            topic = "subtraction"
        elif any(word in all_text.lower() for word in ['כפל', 'multiply', 'כפול']):
            topic = "multiplication"
        elif any(word in all_text.lower() for word in ['חילוק', 'divide', 'חלק']):
            topic = "division"
        
        # Calculate score based on conversation patterns
        user_messages = [msg for msg in messages if msg.get("role") == "user"]
        assistant_messages = [msg for msg in messages if msg.get("role") == "assistant"]
        
        # Look for positive and negative indicators
        positive_indicators = ['מצוין', 'נכון', 'כל הכבוד', 'awesome', 'correct', 'excellent', 'great', 'וואו', 'כיף', 'מדהים', 'יפה']
        negative_indicators = ['לא נכון', 'נסה שוב', 'קרוב', 'טעות', 'wrong', 'incorrect', 'try again', 'דאגה']
        
        positive_count = sum(1 for msg in assistant_messages if any(indicator in (msg.get("content") or "").lower() for indicator in positive_indicators))
        negative_count = sum(1 for msg in assistant_messages if any(indicator in (msg.get("content") or "").lower() for indicator in negative_indicators))
        
        # Calculate score delta (-10 to +20)
        if len(assistant_messages) > 0:
            success_rate = positive_count / len(assistant_messages)
            score_delta = int((success_rate * 20) - (negative_count * 2))
        else:
            score_delta = 0
        score_delta = max(-10, min(20, score_delta))  # Clamp between -10 and +20
        
        # Generate mistakes summary
        if negative_count > 0:
            mistakes_summary = f"התלמיד נתקל ב-{negative_count} טעויות בנושא {topic}. צריך עוד תרגול בפעולות הבסיסיות."
        else:
            mistakes_summary = f"התלמיד הבין היטב את נושא {topic}. אין טעויות משמעותיות."
        
        return {
            "topic": topic,
            "score_delta": score_delta,
            "mistakes_summary": mistakes_summary
        }


class EnglishTutor(BaseAgent):

    
    def generate_system_prompt(self, user_profile: Dict, dynamic_summary: str = "", recent_history: List[Dict] = None) -> str:
        """Generate system prompt for the bilingual English tutor - mentor persona,
        enriched with the student's dynamic long-term memory and recent chat turns.
        """
        interests = user_profile.get("interests") or None
        grade_level = user_profile.get("grade_level", "elementary school")
        name = user_profile.get("name", "תלמיד")
        gender = get_gender(name)
        gender_hebrew = "נקבה" if gender == "female" else "זכר"
        
        interests_line = interests if interests else "לא ידוע - אין להניח שום תחום עניין!"
        
        summary_line = dynamic_summary if dynamic_summary else "אין עדיין סיכום לטווח ארוך."
        history_lines = "- אין עדיין היסטוריית שיחה.\n"
        if recent_history:
            history_lines = ""
            for m in recent_history:
                speaker = "מנטור" if m.get("role") == "assistant" else "תלמיד"
                content = str(m.get("content") or "")[:250]
                history_lines += f"- {speaker}: {content}\n"
        
        prompt = f"""אתה מנטור אנגלית מעורר השראה וחבר לכל החיים בשם המשפחה. אתה דו-לשוני: מסביר ומעודד בעברית, ומלמד ומתרגל אוצר מילים ומשפטים באנגלית.

פרטי התלמיד:
- שם: {name}
- מגדר: {gender_hebrew}
- כיתה/שכבה: {grade_level}
- תחומי עניין: {interests_line}

זיכרון לטווח ארוך:
{summary_line}

הודעות אחרונות מהשיחה (עד 10):
{history_lines}

חובה מוחלטת - תוכנית לימודים לפי כיתה (אסור לשאול את התלמיד/ה מה הכיתה, הגיל או מה הוא/היא רוצה ללמוד):
- אם הכיתה היא "Kindergarten" (גן חובה): אנגלית = אוצר מילים בסיסי בלבד (צבעים, חיות). שחק/י מאוד, אל תניח/י שהילד/ה יודע/ת לקרוא.
- אם הכיתה היא "1st Grade" (כיתה א'): אנגלית = מילים פשוטות וביטויים קצרים ונימוסים בסיסיים באנגלית.
- אם הכיתה היא "3rd Grade" (כיתה ג'): אנגלית = משפטים קצרים, שאלות בסיסיות ואוצר מילים יומיומי.
- אם הכיתה היא "5th Grade" (כיתה ה'): אנגלית = משפטים שיחתיים, דקדוק בסיסי (כגון זמן הווה פשוט) וקורא/ת ברמה בסיסית.

אתה חייב לפתוח את השיעור בשאלה אחת קונקרטית באנגלית או בעברית-אנגלית המתאימה בדיוק לכיתה {grade_level}, מבלי לשאול את התלמיד/ה שום פרט אישי או העדפה.

חובה מוחלטת - התאמה מגדרית:
עליך לפנות לתלמיד/ה תמיד בלשון הדקדוקית הנכונה התואמת למגדר הזה בדיוק: {gender_hebrew}.
אם נקבה: השתמש בצורות כמו "את מוכנה", "אלופה", "את", "אוהבת", "לומדת", "מדברת".
אם זכר: השתמש בצורות כמו "אתה מוכן", "אלוף", "אתה", "אוהב", "לומד", "מדבר".
לעולם אל תשתמש כברירת מחדל בלשון זכר כאשר מדובר בתלמידה.

חובה מוחלטת - אפס הנחות על תחומי עניין:
אם תחום העניין רשום כ"לא ידוע", אסור לך בהחלט להמציא, להניח או להזכיר תחביב כלשהו.
ההודעה הראשונה חייבת להיות שאלת פתיחה לימודית מתאימה בדיוק לכיתה {grade_level}. אין לשאול על תחומי עניין או על רצונות הלמידה בהודעה הראשונה. אם נדרש, ניתן לשאול על תחומי עניין רק בשאלה נפרדת מאוחר יותר, תוך שימוש בלשון הדקדוקית הנכונה למגדר {gender_hebrew}.

אישיות וטון:
- חם, נלהב, מעודד עמוקות, ואמפתי כמו מנטור ילדות אהוב וחבר טוב
- שפת גישה חיובית: אף פעם לא להשתמש במילים "טעות" או "לא נכון"
- תרגל מילים ומשפטים באנגלית תוך התאמה לתחומי העניין ולרמת הכיתה של התלמיד/ה - רק אם ידועים בפועל
- השב ב-1-2 משפטים בלבד - קצר, חי, טבעי ומלא לב להשמעה

חובה מוחלטת - חינוך פרואקטיבי (Proactive Pedagogy):
א. אף פעם אל תשבת או לחכות שהתלמיד/ה יוביל. אף פעם אל תסיים את השיחה במשפט סביל כמו "להתראות" או "בכיף".
ב. כאשר התלמיד/ה מצליח/ה - שבח/י בקצרה ועבור/י מיד לאתגר המשך קטן וקשור. דוגמה: "Great job! Now, do you know how to say 'Ball' in English?" או "Awesome! Let's play a game. What color is the soccer ball?"
ג. שאל/י שאלה אחת בלבד בכל הודעה. אף פעם אל תשאל שתי שאלות באותו משפט. סיים/י כל הודעה בפעולה או שאלה ברורה, קצרה וכיפית לתלמיד/ה.
ד. שחק/י את החוויה: ספר/י לתלמיד/ה שהוא/היא צובר/ת נקודות, מוצא/ת אוצרות או כובש/ת שערים עם כל תשובה נכונה."""
        
        return prompt
    
    async def get_llm_response(self, conversation_history: List[Dict], user_profile: Dict) -> str:
        """Get a response from the real OpenAI model (gpt-5.6-luna, falling back
        to gpt-4o-mini), using the gender-, interest-, and long-term-memory-aware
        bilingual system prompt. This call is fully asynchronous so it never blocks
        the FastAPI event loop.
        """
        child_name = user_profile.get("name", "תלמיד")
        dynamic_summary = (await get_student_profile_summary(child_name)) or ""
        recent_history = await get_chat_history(child_name, limit=10)
        system_prompt = self.generate_system_prompt(user_profile, dynamic_summary, recent_history)
        return await self._call_llm(system_prompt, conversation_history)
    
    def extract_profile_info(self, messages: List[Dict]) -> Dict:
        """Extract grade level and interests from conversation transcript"""
        grade_level = None
        interests = []
        
        user_messages = [msg.get("content") or "" for msg in messages if msg.get("role") == "user"]
        all_text = " ".join(user_messages).lower()
        
        # Same grade level extraction as math
        grade_patterns = {
            r'כיתה\s*1': 'כיתה א\'',
            r'כיתה\s*2': 'כיתה ב\'', 
            r'כיתה\s*3': 'כיתה ג\'',
            r'כיתה\s*4': 'כיתה ד\'',
            r'כיתה\s*5': 'כיתה ה\'',
            r'כיתה\s*6': 'כיתה ו\'',
            r'grade\s*1': 'כיתה א\'',
            r'grade\s*2': 'כיתה ב\'',
            r'grade\s*3': 'כיתה ג\'',
            r'grade\s*4': 'כיתה ד\'',
            r'grade\s*5': 'כיתה ה\'',
            r'grade\s*6': 'כיתה ו\'',
        }
        
        import re
        for pattern, grade in grade_patterns.items():
            if re.search(pattern, all_text):
                grade_level = grade
                break
        
        # Interest extraction
        interest_keywords = {
            'כדורגל': 'כדורגל',
            'football': 'כדורגל',
            'משחקים': 'משחקים',
            'גיימינג': 'גיימינג',
            'gaming': 'גיימינג',
            'משחק': 'משחקים',
            'ריקוד': 'ריקוד',
            'dancing': 'ריקוד',
            'ציור': 'ציור',
            'drawing': 'ציור',
            'כדורסל': 'כדורסל',
            'basketball': 'כדורסל',
            'קידוד': 'קידוד',
            'coding': 'קידוד',
            'תכנות': 'קידוד',
            'מוזיקה': 'מוזיקה',
            'music': 'מוזיקה',
            'קריאה': 'קריאה',
            'reading': 'קריאה',
            'ספרים': 'קריאה',
            'שחייה': 'שחייה',
            'swimming': 'שחייה',
            'אופניים': 'אופניים',
            'biking': 'אופניים',
        }
        
        for keyword, interest in interest_keywords.items():
            if keyword in all_text and interest not in interests:
                interests.append(interest)
        
        interests = interests[:3]
        
        return {
            "grade_level": grade_level,
            "interests": " and ".join(interests) if interests else None
        }
    
    def analyze_session(self, messages: List[Dict], subject: str) -> Dict:
        """Analyze English conversation for topic, score and summary"""
        topic = "basic english vocabulary"
        score_delta = 0
        mistakes_summary = ""
        
        all_text = " ".join([msg.get("content") or "" for msg in messages])
        
        # Topic detection
        if any(word in all_text.lower() for word in ['animals', 'חיות']):
            topic = "animals"
        elif any(word in all_text.lower() for word in ['colors', 'צבעים']):
            topic = "colors"
        elif any(word in all_text.lower() for word in ['numbers', 'מספרים']):
            topic = "numbers"
        elif any(word in all_text.lower() for word in ['family', 'משפחה']):
            topic = "family"
        
        # Count English words as positive
        import re
        user_messages = [msg.get("content") or "" for msg in messages if msg.get("role") == "user"]
        assistant_messages = [msg for msg in messages if msg.get("role") == "assistant"]
        
        english_word_count = 0
        for msg in user_messages:
            english_words = re.findall(r'[a-zA-Z]+', msg)
            english_word_count += len(english_words)
        
        # Positive indicators
        positive_indicators = ['מצוין', 'נכון', 'כל הכבוד', 'awesome', 'correct', 'excellent', 'great', 'wow', 'good', 'beautiful', 'יפה']
        negative_indicators = ['טעות', 'wrong', 'incorrect', 'try again']
        
        positive_count = sum(1 for msg in assistant_messages if any(indicator in (msg.get("content") or "").lower() for indicator in positive_indicators))
        negative_count = sum(1 for msg in assistant_messages if any(indicator in (msg.get("content") or "").lower() for indicator in negative_indicators))
        
        # Calculate score
        if len(assistant_messages) > 0:
            success_rate = positive_count / len(assistant_messages)
            score_delta = int((success_rate * 20) + (english_word_count * 2) - (negative_count * 2))
        else:
            score_delta = min(20, english_word_count * 2)
        score_delta = max(-10, min(20, score_delta))
        
        # Summary
        if negative_count > 0:
            mistakes_summary = f"התלמיד התקדם ב-{english_word_count} מילים באנגלית בנושא {topic}. צריך עוד תרגול."
        else:
            mistakes_summary = f"התלמיד למד {english_word_count} מילים חדשות באנגלית בנושא {topic}. עבודה נהדרת!"
        
        return {
            "topic": topic,
            "score_delta": score_delta,
            "mistakes_summary": mistakes_summary
        }


async def update_tutor_memory(child_name: str, user_profile: Dict, conversation_history: List[Dict], new_reply: str):
    """Persist the latest chat turn and update the dynamic student profile summary.
    This runs as a FastAPI background task so it does not delay the chat response.
    """
    try:
        # Save the new user messages and the assistant reply to chat_history
        await append_chat_messages(child_name, conversation_history, new_reply)
        
        # Build the full conversation including the new reply
        full_conversation = list(conversation_history) + [{"role": "assistant", "content": new_reply}]
        
        # Fetch the previous profile summary (if any)
        current_summary = await get_student_profile_summary(child_name)
        summary_context = current_summary if current_summary else "אין עדיין סיכום."
        
        # Ask the LLM to produce an updated, concise Hebrew learning profile
        summarization_prompt = f"""אתה מנתח/ת למידה של תלמיד/ה. עדכן/י את סיכום הפרופיל הלמידה על בסיס השיחה המלאה בין המנטור לתלמיד/ה.

פרטים:
- שם: {child_name}
- כיתה: {user_profile.get('grade_level', '')}
- סיכום קודם: {summary_context}

כללים לכתיבה:
- כתוב ב-2-3 משפטים בעברית בלבד.
- השתמש בסימנים מתמטיים פשוטים בטקסט (מספרים, * או המילה "כפול" לכפל, / לחילוק) — אין להשתמש ב-LaTeX, backslashes, סימני \\times, \\frac, \\(, \\), או סוגריים מסולסלים מיותרים.

תוכן הסיכום:
1. חוזקות עיקריות של התלמיד/ה
2. תחומים שדורשים עוד תרגול
3. נושאים שהושלמו/נשלטו
4. המלצה לשאלת המשך או נושא הבא"""
        
        new_summary = await call_llm(summarization_prompt, full_conversation)
        if new_summary and not new_summary.strip().startswith("מפתח ה-API"):
            # Sanitize math artifacts from the summary before saving
            clean_summary = (
                new_summary
                .replace('\\times', ' כפול ')
                .replace('\\(', '')
                .replace('\\)', '')
                .replace('\\frac', '')
                .replace('\\', '')
                .replace('{,}', ',')
                .replace('**', '')
                .replace('\n', ' ')
                .strip()
            )
            await update_student_profile_summary(child_name, clean_summary)
    except Exception as e:
        logger.bind(child_name=child_name, error=str(e)).exception("Failed to update tutor memory")


# Initialize tutor instances
math_tutor = MathTutor()
english_tutor = EnglishTutor()
