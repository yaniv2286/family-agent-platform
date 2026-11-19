"""English course catalog.

The parent assigns a course to each child; the course is never inferred from
age or grade. Courses follow the CEFR ladder, and every child may be placed
in any of them.

Lessons themselves are authored later as JSON files. This module is the
catalog the parent screens and the lesson engine read from, so a course
without lessons yet still appears as "coming soon" instead of an empty hole.
"""
from typing import Dict, List, Optional

from lesson_engine import available_lesson_ids


# A lesson is one sitting. "review" lessons close a unit; "onboarding" lessons
# teach the mechanics (hold Space to talk) before any English content.
LESSON_KINDS = frozenset({"onboarding", "teach", "story", "review"})

# Letters are taught in phonics order, never alphabetical order, because this
# order produces readable words earliest (after T/A/B a child can read
# "at" and "bat"). Every letter is anchored to a Hebrew letter the child
# already knows, or flagged as a sound Hebrew does not have.
LETTERS = [
    {"id": "t", "glyph": "T t", "hebrew": "ט", "note": "כמעט כמו ט, עם קצת אוויר"},
    {"id": "a", "glyph": "A a", "hebrew": None, "note": "צליל שאין בעברית - כמו 'אַ' קצרה, לא 'אֶ'"},
    {"id": "b", "glyph": "B b", "hebrew": "ב", "note": "כמו ב, אבל עם קול מלא"},
    {"id": "m", "glyph": "M m", "hebrew": "מ", "note": "בדיוק כמו מ"},
    {"id": "p", "glyph": "P p", "hebrew": "פ", "note": "כמו פ, עם קצת אוויר"},
    {"id": "d", "glyph": "D d", "hebrew": "ד", "note": "כמו ד. נראית כמו b הפוכה - צריך תרגול"},
    {"id": "e", "glyph": "E e", "hebrew": None, "note": "צליל שאין בעברית - 'אֶ' קצרה"},
    {"id": "n", "glyph": "N n", "hebrew": "נ", "note": "בדיוק כמו נ"},
    {"id": "s", "glyph": "S s", "hebrew": "ס", "note": "כמו ס"},
    {"id": "g", "glyph": "G g", "hebrew": "ג", "note": "כמו ג"},
    {"id": "i", "glyph": "I i", "hebrew": None, "note": "צליל שאין בעברית - 'אִ' קצרה"},
    {"id": "f", "glyph": "F f", "hebrew": "פ", "note": "כמו פ בלי דגש, אבל השפתיים נושכות"},
    {"id": "w", "glyph": "W w", "hebrew": None, "note": "צליל שאין בעברית - השפתיים עגולות"},
]

# Recurring letter-friends. Original characters, one per first letter taught.
LETTER_FRIENDS = [
    {"letter": "t", "name": "Tomi", "hebrew_name": "טומי"},
    {"letter": "a", "name": "Ani", "hebrew_name": "אני"},
    {"letter": "b", "name": "Benny", "hebrew_name": "בני"},
    {"letter": "m", "name": "Mimi", "hebrew_name": "מימי"},
    {"letter": "p", "name": "Pip", "hebrew_name": "פיפ"},
    {"letter": "d", "name": "Dudi", "hebrew_name": "דודי"},
]


def _lesson(lesson_id: str, title_he: str, blurb_he: str, kind: str,
            skills: Optional[List[str]] = None, cefr: Optional[str] = None) -> Dict:
    if kind not in LESSON_KINDS:
        raise ValueError(f"unknown lesson kind: {kind}")
    return {
        "lesson_id": lesson_id,
        "title_he": title_he,
        "blurb_he": blurb_he,
        "kind": kind,
        "skills": skills or [],
        "cefr": cefr,
        # Filled in once the JSON lesson file exists; until then the catalog
        # shows the lesson but will not let it start.
        "available": False,
    }


def _unit(unit_id: str, title_he: str, blurb_he: str, lessons: List[Dict]) -> Dict:
    return {
        "unit_id": unit_id,
        "title_he": title_he,
        "blurb_he": blurb_he,
        "lessons": lessons,
    }


# Two lessons a day is the product default (same idea as Lexi's cap). It is a
# pacing choice, separate from the anti-cheat points cap, and a parent can
# override it per child.
DEFAULT_DAILY_LESSON_LIMIT = 2


COURSES: List[Dict] = [
    {
        "course_id": "pre_a",
        "cefr": "Pre-A",
        "title_he": "צעדים ראשונים באנגלית",
        "title_en": "First Steps in English",
        "blurb_he": (
            "מתאים לכל גיל. צבעים, מספרים, ויותר מ-60 מילים ראשונות. "
            "כל אות מגיעה עם חבר, ומחוברת לאות עברית שהילד כבר מכיר."
        ),
        "daily_lesson_limit": DEFAULT_DAILY_LESSON_LIMIT,
        "units": [
            _unit(
                "pre_a.letters",
                "מילים ואותיות ראשונות",
                "פגשו את טומי, אני ובני. למדו צבעים, מספרים 1-8, ואת האותיות T, A, B.",
                [
                    _lesson(
                        "pre_a.letters.00_hello",
                        "לפני שנתחיל",
                        "נפגוש את המורה, נלמד להחזיק את מקש הרווח כדי לדבר, ונגיד שלום.",
                        "onboarding",
                        ["english.speaking.turn_taking"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.01_backyard",
                        "בחצר",
                        "הצבעים red, blue, yellow, green, ספירה מאחת עד ארבע, ו-My name is.",
                        "teach",
                        ["english.vocabulary.colors", "english.vocabulary.numbers"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.02_backyard_2",
                        "בחצר (חלק 2)",
                        "פוגשים את אני. ant ו-apple, וסופרים עד שמונה.",
                        "teach",
                        ["english.phonics.a", "english.vocabulary.numbers"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.03_toybox",
                        "קופסת הצעצועים",
                        "ball ו-toy box, ועוד שלושה צבעים: pink, orange, purple. הצלילים A ו-B.",
                        "teach",
                        ["english.phonics.b", "english.vocabulary.colors"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.04_tomi",
                        "טומי בא לשחק",
                        "פוגשים את טומי והנמר שלו. tiger, table, teapot, הצבע black, ו-I have.",
                        "teach",
                        ["english.phonics.t", "english.sentences.i_have"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.05_bedroom",
                        "בחדר השינה",
                        "mat, bed, window, door, desk, telephone. כותבים את האות T.",
                        "teach",
                        ["english.phonics.t", "english.writing.letters"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.06_on_the_bed",
                        "על המיטה",
                        "alligator, blanket, pillow, book, apple, ambulance. משחקי חזרה מהירה.",
                        "teach",
                        ["english.vocabulary.objects"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.07_letter_a",
                        "על המיטה (חלק 2)",
                        "האות A: מילים שמתחילות בצליל שלה, וכתיבה של A גדולה ו-a קטנה.",
                        "teach",
                        ["english.phonics.a", "english.writing.letters"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.08_under_the_bed",
                        "מתחת למיטה",
                        "bat, map, dinosaur, pajamas, monkey.",
                        "teach",
                        ["english.vocabulary.objects"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.09_letter_b",
                        "מתחת למיטה (חלק 2)",
                        "האות B: balloon, ball, blanket, bat, bed, box. קוראים את המילים הראשונות: bat ו-tab.",
                        "story",
                        ["english.phonics.b", "english.reading.decodable"],
                        "Pre-A",
                    ),
                    _lesson(
                        "pre_a.letters.10_review",
                        "מסתכלים דרך החלון",
                        "חוזרים על כל המילים והאותיות. ממיינים מילים לפי הצליל הראשון, וקוראים bat ו-tab.",
                        "review",
                        ["english.phonics.a", "english.phonics.b", "english.phonics.t"],
                        "Pre-A",
                    ),
                ],
            ),
        ],
    },
    {
        "course_id": "a1",
        "cefr": "A1",
        "title_he": "אנגלית למתחילים",
        "title_en": "Beginner English",
        "blurb_he": (
            "משפטים ראשונים, שיחות קצרות וקריאה. היחידה הראשונה: נעים להכיר - "
            "מציגים את עצמנו, לומדים רגשות, ופוגשים משפחה."
        ),
        "daily_lesson_limit": DEFAULT_DAILY_LESSON_LIMIT,
        "units": [
            _unit(
                "a1.getting_to_know_you",
                "נעים להכיר",
                "פגשו את המורה, את המשפחה שלה ואת אחיה. למדו להציג את עצמכם ולומר איך אתם מרגישים.",
                [
                    _lesson(
                        "a1.gtky.00_hello",
                        "לפני שנתחיל",
                        "נפגוש את המורה, נתרגל החזקת מקש הרווח, ונתחיל לדבר.",
                        "onboarding",
                        ["english.speaking.turn_taking"],
                        "A1",
                    ),
                    _lesson(
                        "a1.gtky.01_meet_you",
                        "להכיר אותך",
                        "מה שמך, מה שלומך, ואיך אתה מרגיש. פוגשים את ליאו ואת לוסי שלנו.",
                        "teach",
                        ["english.sentences.introductions", "english.vocabulary.feelings"],
                        "A1",
                    ),
                    _lesson(
                        "a1.gtky.02_my_family",
                        "פגוש את המשפחה שלי",
                        "מילים על בני משפחה, סיפור קצר, ומשפטים נכונים ולא נכונים.",
                        "story",
                        ["english.vocabulary.family", "english.reading.basic"],
                        "A1",
                    ),
                    _lesson(
                        "a1.gtky.03_review",
                        "סקירת שיעור",
                        "שאלות, משחקי דיבור, ואתגר אוצר מילים על כל מה שלמדנו.",
                        "review",
                        ["english.sentences.introductions", "english.vocabulary.feelings", "english.vocabulary.family"],
                        "A1",
                    ),
                ],
            ),
        ],
    },
    {
        "course_id": "a2",
        "cefr": "A2",
        "title_he": "אנגלית לרמת ביניים",
        "title_en": "Elementary English",
        "blurb_he": (
            "סיפורים, הבנת הנקרא ודקדוק: present simple, past simple, וכתיבת משפטים. "
            "היחידה הראשונה חוזרת על 'נעים להכיר' בעומק של כיתה גבוהה."
        ),
        "daily_lesson_limit": DEFAULT_DAILY_LESSON_LIMIT,
        "units": [
            _unit(
                "a2.getting_to_know_you",
                "נעים להכיר",
                "מציגים את עצמנו במשפטים מלאים, קוראים סיפור קצר, ומתקנים משפטים בעבר.",
                [
                    _lesson(
                        "a2.gtky.00_hello",
                        "לפני שנתחיל",
                        "נפגוש את המורה ונתרגל שיחה חופשית עם מקש הרווח.",
                        "onboarding",
                        ["english.speaking.turn_taking"],
                        "A2",
                    ),
                    _lesson(
                        "a2.gtky.01_about_me",
                        "ספרו עליי",
                        "משפטי הצגה מלאים, רגשות עם 'because', וזמן הווה פשוט.",
                        "teach",
                        ["english.present_simple", "english.vocabulary.feelings"],
                        "A2",
                    ),
                    _lesson(
                        "a2.gtky.02_family_story",
                        "הסיפור של המשפחה שלי",
                        "קוראים סיפור, מתקנים משפטים שגויים, וכותבים משפט על המשפחה שלנו.",
                        "story",
                        ["english.past_simple", "english.reading.basic", "english.writing.basic"],
                        "A2",
                    ),
                    _lesson(
                        "a2.gtky.03_review",
                        "סקירת שיעור",
                        "אתגרי דיבור, כתיבה ואוצר מילים על כל היחידה.",
                        "review",
                        ["english.present_simple", "english.past_simple", "english.writing.basic"],
                        "A2",
                    ),
                ],
            ),
        ],
    },
]


def _with_availability(course: Dict) -> Dict:
    ready = set(available_lesson_ids())
    units = []
    for unit in course["units"]:
        lessons = [{**lesson, "available": lesson["lesson_id"] in ready} for lesson in unit["lessons"]]
        units.append({**unit, "lessons": lessons})
    return {**course, "units": units}


def list_courses(include_unavailable: bool = True) -> List[Dict]:
    """Catalog for the parent screen.

    Lessons without content are marked, so the parent sees the whole plan
    rather than only what is built.
    """
    courses = []
    for course in (_with_availability(c) for c in COURSES):
        units = []
        for unit in course["units"]:
            lessons = unit["lessons"] if include_unavailable else [
                lesson for lesson in unit["lessons"] if lesson["available"]
            ]
            units.append({**unit, "lessons": lessons, "lesson_count": len(unit["lessons"]),
                          "available_count": sum(1 for lesson in unit["lessons"] if lesson["available"])})
        courses.append({**course, "units": units})
    return courses


def get_course(course_id: str) -> Optional[Dict]:
    course = next((c for c in COURSES if c["course_id"] == course_id), None)
    return _with_availability(course) if course else None


def get_lesson(lesson_id: str) -> Optional[Dict]:
    for course in COURSES:
        for unit in course["units"]:
            for lesson in unit["lessons"]:
                if lesson["lesson_id"] == lesson_id:
                    return {**lesson, "course_id": course["course_id"], "unit_id": unit["unit_id"]}
    return None


def course_ids() -> List[str]:
    return [course["course_id"] for course in COURSES]
