"""Runs a scripted English lesson, one slide at a time.

The lesson file is the curriculum; this module only decides what the child
sees and whether an answer is right. It never invents content, so a tutor
cannot drift into a different lesson or mark a wrong answer correct.

Slides:
  speak   the tutor says a line and the child repeats it
  letter  a letter card with a Hebrew sound anchor; the child repeats
          the sound (graded exactly like speak)
  choice  pick the matching card (picture, word, or sentence)
  listen  the tutor says a word and the child picks what they heard
  true_false  a sentence about a story, then the correction
  order   tap words into the right sentence

Optional visuals on any slide/option:
  image   emoji char or an image URL, shown as a big card
  hebrew_anchor (letter slides) e.g. "B זה ב' בעברית — כמו בלון"

A slide that needs a spoken answer ("speak") is graded leniently, because
Whisper garbles children's English; it checks the key words, not the spelling.
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

LESSONS_DIR = Path(__file__).parent / "lessons" / "english"

# Sounds a Hebrew speaker mixes up. Used only to accept a near-miss as a real
# attempt, never to mark it correct.
_CONFUSIONS = str.maketrans({"b": "p", "p": "b", "v": "b", "i": "e", "a": "e"})


def _norm(text: str) -> str:
    return re.sub(r"[^a-z' ]", " ", (text or "").lower()).strip()


def _words(text: str) -> List[str]:
    return [w for w in _norm(text).split() if w]


def load_lesson(lesson_id: str) -> Optional[Dict]:
    """Load a lesson JSON by id. Returns None when it has not been authored."""
    if not lesson_id or not re.fullmatch(r"[a-z0-9_.]+", lesson_id):
        return None
    path = LESSONS_DIR / f"{lesson_id}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("lesson_id") != lesson_id:
        raise ValueError(f"{path.name} declares {data.get('lesson_id')}, not {lesson_id}")
    return data


def available_lesson_ids() -> List[str]:
    if not LESSONS_DIR.is_dir():
        return []
    ids = []
    for path in sorted(LESSONS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if data.get("lesson_id") == path.stem:
            ids.append(path.stem)
    return ids


def public_slide(slide: Dict) -> Dict:
    """Strip the answer key before a slide reaches the browser."""
    hidden = {"correct", "accept", "expected_order"}
    return {k: v for k, v in slide.items() if k not in hidden}


def learned_words(lesson: Dict) -> List[str]:
    """The English words a lesson actually taught - for the closing ritual.
    speak slides contribute their target; choice/listen slides contribute
    the correct option's label. Letter-card slides contribute the letter."""
    words = []
    for slide in lesson.get("slides") or []:
        kind = slide.get("type")
        if kind == "letter":
            letter = (slide.get("letter") or "").strip()
            if letter:
                words.append(letter)
        elif kind == "speak":
            accept = slide.get("accept") or []
            if accept:
                words.append(str(accept[0]))
        elif kind in ("choice", "listen"):
            correct_id = slide.get("correct")
            for opt in slide.get("options") or []:
                if str(opt.get("id")) == str(correct_id) and opt.get("label"):
                    words.append(str(opt["label"]))
                    break
    seen, out = set(), []
    for w in words:
        key = w.lower()
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out


def next_lesson_meta(lesson_id: str, done_ids: set) -> Optional[Dict]:
    """The next uncompleted lesson after `lesson_id` in its own course,
    skipping over lessons that have no authored content yet."""
    from english_curriculum import get_lesson, get_course
    from lesson_engine import available_lesson_ids
    meta = get_lesson(lesson_id)
    if meta is None:
        return None
    course = get_course(meta["course_id"])
    if course is None:
        return None
    authored = set(available_lesson_ids())
    found_current = False
    for unit in course["units"]:
        for lesson in unit["lessons"]:
            lid = lesson["lesson_id"]
            if lid == lesson_id:
                found_current = True
                continue
            if found_current and lid in authored and lid not in done_ids:
                return {"lesson_id": lid, "title_he": lesson["title_he"]}
    return None


def _close(heard: str, wanted: str) -> bool:
    """A child's pronunciation rarely transcribes letter-perfect."""
    if heard == wanted or heard.translate(_CONFUSIONS) == wanted:
        return True
    if abs(len(heard) - len(wanted)) > 2:
        return False
    return heard[:3] == wanted[:3]


def _score_spoken(said: str, accept: List[str]) -> bool:
    """True when the child produced the target words, however they were spelled."""
    heard = _words(said)
    for target in accept:
        needed = _words(target)
        if needed and all(any(_close(h, w) for h in heard) for w in needed):
            return True
    return False


def grade(slide: Dict, answer) -> Dict:
    """Grade one answer. Returns correct, a short praise or hint, and the
    canonical right answer so the screen can reveal it."""
    kind = slide.get("type")
    if kind in ("speak", "letter"):
        correct = _score_spoken(str(answer or ""), slide.get("accept") or [])
        revealed = slide["accept"][0]
    elif kind in ("choice", "listen"):
        correct = str(answer) == str(slide.get("correct"))
        revealed = slide.get("correct")
    elif kind == "true_false":
        verdict = str(answer).lower() in ("true", "1", "yes", "נכון")
        correct = verdict == bool(slide.get("correct"))
        revealed = "נכון" if slide.get("correct") else "לא נכון"
    elif kind == "order":
        given = [_norm(w) for w in (answer or [])]
        expected = [_norm(w) for w in slide.get("expected_order") or []]
        correct = given == expected
        revealed = " ".join(slide.get("expected_order") or [])
    else:
        raise ValueError(f"slide {slide.get('id')} has no answer")

    return {
        "correct": correct,
        "praise_he": slide.get("praise_he") if correct else slide.get("hint_he"),
        "reveal": revealed,
        "star": correct,
    }
