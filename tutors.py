import os
from typing import List, Dict, Optional
from dotenv import load_dotenv
from openai import OpenAI
from database import SessionLocal, User

# Ensure environment variables from .env are loaded even if this module is
# imported before database.py (which also calls load_dotenv()).
load_dotenv()

# Model is locked to gpt-4o-mini for cost optimization. Do NOT change this
# to gpt-4 or gpt-4o without an explicit, deliberate decision.
OPENAI_MODEL = "gpt-4o-mini"

_openai_client: Optional[OpenAI] = None


def _get_openai_client() -> Optional[OpenAI]:
    """Lazily create (and cache) the OpenAI client if a real API key is configured.

    Returns None if no valid key is present, so callers can gracefully fall
    back to the local mock response engine.
    """
    global _openai_client
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your_openai_api_key_here":
        return None
    if _openai_client is None:
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def call_llm(system_prompt: str, conversation_history: List[Dict]) -> Optional[str]:
    """Call the real OpenAI gpt-4o-mini model with the given system prompt and
    conversation history. Returns the assistant's reply text, or None if the
    LLM is not configured or the call fails for any reason (network error,
    invalid key, rate limit, etc.) - in which case the caller should fall
    back to the local mock response engine.
    """
    client = _get_openai_client()
    if client is None:
        return None

    messages = [{"role": "system", "content": system_prompt}]
    for msg in conversation_history:
        role = msg.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        messages.append({"role": role, "content": msg.get("content", "")})

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.8,
            max_tokens=150,
        )
        reply = response.choices[0].message.content
        return reply.strip() if reply else None
    except Exception as e:
        print(f"OpenAI API call failed, falling back to mock engine: {e}")
        return None


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


class MathTutor:
    def __init__(self):
        self.attempt_counts = {}  # Track consecutive incorrect attempts per user session
        self.victory_memory = {}  # Store memorable wins per user
        self.frustration_indicators = ['עייף', 'קשה', 'לא מבין', 'מת', 'אני לא יכול', 'אני עייף', 'לא רוצה', 'שונא', 'מסכים']
    
    def get_user_profile(self, user_id: int) -> Optional[Dict]:
        """Get user profile from database"""
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                return {
                    "id": user.id,
                    "name": user.name,
                    "role": user.role,
                    "grade_level": user.grade_level,
                    "interests": user.interests
                }
            return None
        finally:
            db.close()
    
    def generate_system_prompt(self, user_profile: Dict) -> str:
        """Generate system prompt based on user profile - mentor persona"""
        interests = user_profile.get("interests") or None
        grade_level = user_profile.get("grade_level", "elementary school")
        name = user_profile.get("name", "תלמיד")
        gender = get_gender(name)
        gender_hebrew = "נקבה" if gender == "female" else "זכר"
        
        # Get victory memory for this user
        victories = self.victory_memory.get(user_profile.get("id", ""), [])
        victory_context = ""
        if victories:
            victory_context = f"ניצחונות קודמים: {', '.join(victories[:3])}"
        
        interests_line = interests if interests else "לא ידוע - אין להניח שום תחום עניין!"
        
        prompt = f"""אתה מנטור מתמטיקה מעורר השראה וחבר לכל החיים בשם המשפחה. אתה מדבר בעברית בלבד.

פרטי התלמיד:
- שם: {name}
- מגדר: {gender_hebrew}
- רמה: {grade_level}
- תחומי עניין: {interests_line}
{victory_context}

חובה מוחלטת - התאמה מגדרית:
עליך לפנות לתלמיד/ה תמיד בלשון הדקדוקית הנכונה התואמת למגדר: {gender_hebrew}.
אם נקבה: השתמש במילים כמו "מוכנה", "את", "אלופה", "אוהבת", "לומדת".
אם זכר: השתמש במילים כמו "מוכן", "אתה", "אלוף", "אוהב", "לומד".
לעולם אל תשתמש כברירת מחדל בלשון זכר כאשר מדובר בתלמידה.

חובה מוחלטת - אפס הנחות על תחומי עניין:
אם תחומי העניין אינם ידועים (רשום "לא ידוע"), אסור לך בהחלט להניח או להמציא תחום עניין כלשהו (כגון כדורגל, ריקוד וכו').
במקרה כזה, ההודעה הראשונה שלך חייבת לשאול את התלמיד/ה במפורש מה הוא/היא הכי אוהב/ת לעשות בזמן הפנוי, תוך שימוש בלשון הדקדוקית הנכונה למגדר {gender_hebrew}.

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
6. השתמש בסימון מתמטי פשוט שמתאים לרמת הכיתה"""
        
        return prompt
    
    def get_llm_response(self, conversation_history: List[Dict], user_profile: Dict) -> str:
        """Get a response from the real OpenAI gpt-4o-mini model, using the
        gender- and interest-aware system prompt. Falls back to the local
        mock engine if the LLM is not configured or the call fails.
        """
        system_prompt = self.generate_system_prompt(user_profile)
        reply = call_llm(system_prompt, conversation_history)
        if reply is not None:
            return reply
        
        # Fallback to mock engine
        user_input = conversation_history[-1].get("content", "") if conversation_history else ""
        return self.get_mock_response(user_input, conversation_history, user_profile)
    
    def get_mock_response(self, user_input: str, conversation_history: List[Dict], user_profile: Dict) -> str:
        """Generate mock responses with inspiring mentor persona - gender-aware, zero interest assumptions"""
        user_id = user_profile["id"]
        gender = get_gender(user_profile.get("name", ""))
        bo = g("בוא", gender)
        atah = g("אתה", gender)
        ohev = g("אוהב", gender)
        ohed = g("אוהד", gender)
        lomed = g("לומד", gender)
        chaver = g("חבר", gender)
        tirtze = g("תרצה", gender)
        
        # Check for frustration indicators
        user_input_lower = user_input.lower()
        is_frustrated = any(indicator in user_input_lower for indicator in self.frustration_indicators)
        
        # Emotional support for frustration
        if is_frustrated:
            return f"הכל בסדר, מותר לקחת רגע! {bo} נפתור רק עוד אחד קטן ביחד ונצא אלופים!"
        
        # Check if this is first session (missing profile data) - NEVER assume interests
        needs_onboarding = (
            not user_profile.get("grade_level") or 
            user_profile.get("grade_level") in [None, "", "Unknown"] or
            not user_profile.get("interests") or 
            user_profile.get("interests") in [None, "", "Unknown"]
        )
        
        # Onboarding mode - explicitly ask for grade and interests warmly, gender-correct, zero assumptions
        if needs_onboarding:
            if len(conversation_history) <= 1:
                return f"שלום {user_profile.get('name', chaver)}! כל כך כיף ש{atah} כאן! באיזו כיתה {atah} {lomed} ומה {atah} הכי {ohev} לעשות בזמן הפנוי?"
            else:
                return f"מדהים! עכשיו שאני מכיר {'אותך' if gender == 'male' else 'אותך'} טוב יותר, {bo} נצא להרפתק במתמטיקה ביחד!"
        
        # Initialize attempt counter if not exists
        if user_id not in self.attempt_counts:
            self.attempt_counts[user_id] = 0
        
        # Check for help requests with growth mindset
        if any(word in user_input for word in ['עזרה', 'לא מבין', 'קשה', 'איך']):
            self.attempt_counts[user_id] += 1
            if self.attempt_counts[user_id] >= 3:
                self.attempt_counts[user_id] = 0
                # Record this as a learning moment
                self.record_victory(user_id, "התמדה מדהימה")
                return "אני כל כך גאה בך שלא ויתרת! התשובה היא 12, אבל הדרך שלנו לשם הייתה מדהימה!"
            else:
                return f"איזה כיוון יפה! היינו ממש קרוב, {bo} ננסה יחד עוד צעד קטן!"
        
        # Check for numeric answers with celebration
        import re
        numbers = re.findall(r'\d+', user_input)
        if numbers:
            if len(numbers) == 1:
                num = int(numbers[0])
                if num > 0:
                    self.attempt_counts[user_id] = 0
                    # Record victory
                    self.record_victory(user_id, "פתרון מתמטי מוצלח")
                    return "וואו! אנחנו עושים את זה! זה נכון! כל הכבוד על ההתמדה שלך!"
                else:
                    self.attempt_counts[user_id] += 1
                    if self.attempt_counts[user_id] >= 3:
                        self.attempt_counts[user_id] = 0
                        return f"היינו קרובים מאוד! הכיוון שלנו היה יפה, {bo} נחשוב יחד על מספרים חיוביים!"
                    else:
                        return f"איזה כיוון יפה! {bo} נחשוב יחד איך המספרים מתנהגים!"
        
        # Interest-based responses - STRICTLY driven by the actual DB value, never assumed.
        # If interests is empty this branch is unreachable (onboarding handles it above).
        interests = (user_profile.get("interests") or "").lower()
        victories = self.victory_memory.get(user_id, [])
        
        if "football" in interests or "כדורגל" in interests:
            if "כדורגל" in str(victories):
                return f"זוכר/ת איך פתרנו את הבעיה עם כדורגל? {bo} נעשה את זה שוב!"
            return f"{ohed} כדורגל? אנחנו חושבים על בעיה שקשורה למשחק! כמה שחקנים בקבוצה שלנו?"
        elif "gaming" in interests or "משחקים" in interests:
            if "משחקים" in str(victories):
                return "כמו במשחקים, אנחנו מתקדמים רמה אחר רמה!"
            return f"{ohev} משחקים? אנחנו עולים רמה! איזה אתגר חדש {tirtze}?"
        elif "dancing" in interests or "ריקוד" in interests:
            return f"ריקודים זה כיף! אנחנו רוקדים עם מספרים! כמה צעדים נעשה ביחד?"
        elif "coding" in interests or "קידוד" in interests:
            return f"קידוד זה מדהים! אנחנו מתכנתים פתרונות! איזה קוד נכתוב ביחד?"
        elif "music" in interests or "מוזיקה" in interests:
            return f"מוזיקה זה יפה! אנחנו יוצרים מנגינה עם מספרים! איזה תו ננגן?"
        elif "reading" in interests or "קריאה" in interests:
            return f"קריאה זה כיף! אנחנו כותבים סיפורים עם מספרים! איזה סיפור נספר?"
        else:
            # Reference previous victories - still never invents a hobby
            if victories:
                return f"זוכר/ת איך ניצחנו ב-{victories[0]}? {bo} נעשה את זה שוב!"
            return f"שלום {chaver}! אנחנו הולכים להיות אלופי מתמטיקה ביחד! איזה נושא נתחיל?"
    
    def record_victory(self, user_id: int, achievement: str):
        """Record a memorable achievement for the child"""
        if user_id not in self.victory_memory:
            self.victory_memory[user_id] = []
        
        if achievement not in self.victory_memory[user_id]:
            self.victory_memory[user_id].append(achievement)
            # Keep only last 5 victories
            if len(self.victory_memory[user_id]) > 5:
                self.victory_memory[user_id] = self.victory_memory[user_id][-5:]
    
    def extract_profile_info(self, messages: List[Dict]) -> Dict:
        """Extract grade level and interests from conversation transcript"""
        grade_level = None
        interests = []
        
        # Combine all user messages for analysis
        user_messages = [msg.get("content", "") for msg in messages if msg.get("role") == "user"]
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
        all_text = " ".join([msg.get("content", "") for msg in messages])
        
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
        
        positive_count = sum(1 for msg in assistant_messages if any(indicator in msg.get("content", "").lower() for indicator in positive_indicators))
        negative_count = sum(1 for msg in assistant_messages if any(indicator in msg.get("content", "").lower() for indicator in negative_indicators))
        
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


class EnglishTutor:
    def __init__(self):
        self.attempt_counts = {}  # Track consecutive incorrect attempts per user session
        self.victory_memory = {}  # Store memorable wins per user
        self.frustration_indicators = ['עייף', 'קשה', 'לא מבין', 'מת', 'אני לא יכול', 'אני עייף', 'לא רוצה', 'שונא', 'מסכים']
    
    def get_user_profile(self, user_id: int) -> Optional[Dict]:
        """Get user profile from database"""
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                return {
                    "id": user.id,
                    "name": user.name,
                    "role": user.role,
                    "grade_level": user.grade_level,
                    "interests": user.interests
                }
            return None
        finally:
            db.close()
    
    def generate_system_prompt(self, user_profile: Dict) -> str:
        """Generate system prompt for the bilingual English tutor - mentor persona"""
        interests = user_profile.get("interests") or None
        grade_level = user_profile.get("grade_level", "elementary school")
        name = user_profile.get("name", "תלמיד")
        gender = get_gender(name)
        gender_hebrew = "נקבה" if gender == "female" else "זכר"
        
        victories = self.victory_memory.get(user_profile.get("id", ""), [])
        victory_context = ""
        if victories:
            victory_context = f"ניצחונות קודמים: {', '.join(victories[:3])}"
        
        interests_line = interests if interests else "לא ידוע - אין להניח שום תחום עניין!"
        
        prompt = f"""אתה מנטור אנגלית מעורר השראה וחבר לכל החיים בשם המשפחה. אתה דו-לשוני: מסביר ומעודד בעברית, ומלמד ומתרגל אוצר מילים ומשפטים באנגלית.

פרטי התלמיד:
- שם: {name}
- מגדר: {gender_hebrew}
- רמה: {grade_level}
- תחומי עניין: {interests_line}
{victory_context}

חובה מוחלטת - התאמה מגדרית:
עליך לפנות לתלמיד/ה תמיד בלשון הדקדוקית הנכונה התואמת למגדר: {gender_hebrew}.
אם נקבה: השתמש במילים כמו "מוכנה", "את", "אלופה", "אוהבת", "לומדת", "מדברת".
אם זכר: השתמש במילים כמו "מוכן", "אתה", "אלוף", "אוהב", "לומד", "מדבר".
לעולם אל תשתמש כברירת מחדל בלשון זכר כאשר מדובר בתלמידה.

חובה מוחלטת - אפס הנחות על תחומי עניין:
אם תחומי העניין אינם ידועים (רשום "לא ידוע"), אסור לך בהחלט להניח או להמציא תחום עניין כלשהו.
במקרה כזה, ההודעה הראשונה שלך חייבת לשאול את התלמיד/ה במפורש (בעברית) מה הוא/היא הכי אוהב/ת לעשות בזמן הפנוי, תוך שימוש בלשון הדקדוקית הנכונה למגדר {gender_hebrew}.

אישיות וטון:
- חם, נלהב, מעודד עמוקות, ואמפתי כמו מנטור ילדות אהוב וחבר טוב
- שפת גישה חיובית: אף פעם לא להשתמש במילים "טעות" או "לא נכון"
- תרגל מילים ומשפטים באנגלית תוך התאמה לתחומי העניין ולרמת הכיתה של התלמיד/ה - רק אם ידועים בפועל
- השב ב-1-2 משפטים בלבד - קצר, חי, טבעי ומלא לב להשמעה"""
        
        return prompt
    
    def get_llm_response(self, conversation_history: List[Dict], user_profile: Dict) -> str:
        """Get a response from the real OpenAI gpt-4o-mini model, using the
        gender- and interest-aware bilingual system prompt. Falls back to the
        local mock engine if the LLM is not configured or the call fails.
        """
        system_prompt = self.generate_system_prompt(user_profile)
        reply = call_llm(system_prompt, conversation_history)
        if reply is not None:
            return reply
        
        # Fallback to mock engine
        user_input = conversation_history[-1].get("content", "") if conversation_history else ""
        return self.get_mock_response(user_input, conversation_history, user_profile)
    
    def get_mock_response(self, user_input: str, conversation_history: List[Dict], user_profile: Dict) -> str:
        """Generate English tutor mock responses with inspiring mentor persona - gender-aware, zero interest assumptions"""
        user_id = user_profile["id"]
        gender = get_gender(user_profile.get("name", ""))
        bo = g("בוא", gender)
        atah = g("אתה", gender)
        ohev = g("אוהב", gender)
        lomed = g("לומד", gender)
        mochan = g("מוכן", gender)
        medaber = g("מדבר", gender)
        chaver = g("חבר", gender)
        
        # Check for frustration indicators
        user_input_lower = user_input.lower()
        is_frustrated = any(indicator in user_input_lower for indicator in self.frustration_indicators)
        
        # Emotional support for frustration
        if is_frustrated:
            return f"הכל בסדר, מותר לקחת רגע! {bo} נתרגל רק עוד מילה אחת ביחד ונצא אלופים!"
        
        # Check if this is first session (missing profile data) - NEVER assume interests
        needs_onboarding = (
            not user_profile.get("grade_level") or 
            user_profile.get("grade_level") in [None, "", "Unknown"] or
            not user_profile.get("interests") or 
            user_profile.get("interests") in [None, "", "Unknown"]
        )
        
        # Onboarding mode - explicitly ask for grade and interests, gender-correct, zero assumptions
        if needs_onboarding:
            if len(conversation_history) <= 1:
                return f"שלום {user_profile.get('name', chaver)}! I'm so happy {atah} are here! באיזו כיתה {atah} {lomed} ומה {atah} הכי {ohev} לעשות בזמן הפנוי?"
            else:
                return f"מדהים! Now we will learn English together in a fun way! {mochan} להתחיל?"
        
        # Initialize attempt counter if not exists
        if user_id not in self.attempt_counts:
            self.attempt_counts[user_id] = 0
        
        # Simple heuristic analysis for English
        # Check for user asking for help or using Hebrew
        if any(word in user_input for word in ['עזרה', 'לא מבין', 'קשה', 'איך']):
            self.attempt_counts[user_id] += 1
            if self.attempt_counts[user_id] >= 3:
                self.attempt_counts[user_id] = 0
                self.record_victory(user_id, "התמדה מדהימה באנגלית")
                return "אני כל כך גאה בך! התשובה הנכונה היא: I love learning English! כל הכבוד שהתמדת!"
            else:
                return "איזה כיוון יפה! Let's try together - say one word in English!"
        
        # Check if the user said something in English
        import re
        english_words = re.findall(r'[a-zA-Z]+', user_input)
        if english_words:
            self.attempt_counts[user_id] = 0
            self.record_victory(user_id, "התקדמות באנגלית")
            english_phrase = ' '.join(english_words)
            return f'Wow! "{english_phrase}" - איזה יפה! {atah} {medaber} אנגלית מצוין! {bo} ננסה עוד משפט!'
        
        # Interest-based responses - STRICTLY driven by the actual DB value, never assumed.
        # If interests is empty this branch is unreachable (onboarding handles it above).
        interests = (user_profile.get("interests") or "").lower()
        victories = self.victory_memory.get(user_id, [])
        
        if "football" in interests or "כדורגל" in interests:
            if "כדורגל" in str(victories):
                return f"Remember how we learned 'ball'? {bo} נלמוד עוד מילה חדשה בכדורגל!"
            return f"{atah} {ohev} כדורגל! {bo} נלמוד את המילה: 'football'. תגיד/י איתי: football!"
        elif "gaming" in interests or "משחקים" in interests:
            return f"{ohev} משחקים? {bo} נלמוד: 'game'. תגיד/י איתי: game!"
        elif "dancing" in interests or "ריקוד" in interests:
            return f"ריקוד זה כיף! {bo} נלמוד: 'dance'. תגיד/י איתי: dance!"
        elif "coding" in interests or "קידוד" in interests:
            return f"קידוד זה מדהים! {bo} נלמוד: 'computer'. תגיד/י איתי: computer!"
        elif "music" in interests or "מוזיקה" in interests:
            return f"מוזיקה זה יפה! {bo} נלמוד: 'music'. תגיד/י איתי: music!"
        elif "reading" in interests or "קריאה" in interests:
            return f"קריאה זה כיף! {bo} נלמוד: 'book'. תגיד/י איתי: book!"
        else:
            # Reference previous victories - still never invents a hobby
            if victories:
                return f"זוכר/ת איך התקדמנו ב-{victories[0]}? {bo} נלמוד עוד מילה!"
            return "Hello friend! We are going to be English stars together! What word would you like to learn?"
    
    def record_victory(self, user_id: int, achievement: str):
        """Record a memorable achievement for the child"""
        if user_id not in self.victory_memory:
            self.victory_memory[user_id] = []
        
        if achievement not in self.victory_memory[user_id]:
            self.victory_memory[user_id].append(achievement)
            if len(self.victory_memory[user_id]) > 5:
                self.victory_memory[user_id] = self.victory_memory[user_id][-5:]
    
    def extract_profile_info(self, messages: List[Dict]) -> Dict:
        """Extract grade level and interests from conversation transcript"""
        grade_level = None
        interests = []
        
        user_messages = [msg.get("content", "") for msg in messages if msg.get("role") == "user"]
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
        
        all_text = " ".join([msg.get("content", "") for msg in messages])
        
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
        user_messages = [msg.get("content", "") for msg in messages if msg.get("role") == "user"]
        assistant_messages = [msg for msg in messages if msg.get("role") == "assistant"]
        
        english_word_count = 0
        for msg in user_messages:
            english_words = re.findall(r'[a-zA-Z]+', msg)
            english_word_count += len(english_words)
        
        # Positive indicators
        positive_indicators = ['מצוין', 'נכון', 'כל הכבוד', 'awesome', 'correct', 'excellent', 'great', 'wow', 'good', 'beautiful', 'יפה']
        negative_indicators = ['טעות', 'wrong', 'incorrect', 'try again']
        
        positive_count = sum(1 for msg in assistant_messages if any(indicator in msg.get("content", "").lower() for indicator in positive_indicators))
        negative_count = sum(1 for msg in assistant_messages if any(indicator in msg.get("content", "").lower() for indicator in negative_indicators))
        
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


# Initialize tutor instances
math_tutor = MathTutor()
english_tutor = EnglishTutor()
