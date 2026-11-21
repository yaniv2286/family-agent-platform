"""Full QA bot: simulates all 4 kids against the REAL system and checks
everything end-to-end before deploy.

Sections (default: all):
  smoke    - endpoint health, auth, latency budgets (no LLM cost)
  chat     - English free-chat simulation per kid (live LLM)
  math     - math chat simulation per kid (live LLM)
  lessons  - structured lesson flow: fetch slides, answer, complete, stars
  perf     - latency report across every request made (part of all sections)

    python scripts/simulate_children.py                  # full QA run
    python scripts/simulate_children.py --section smoke  # fast, no LLM cost
    python scripts/simulate_children.py --kids ליבי --turns 6 --verbose

Requires OPENAI_API_KEY + APP_PIN in .env. Exits 1 on any failure.
"""

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal, User  # noqa: E402
from main import app  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Kids: persona for the simulator LLM + expected tutor behavior
# ---------------------------------------------------------------------------
KIDS = [
    {
        "name": "ליבי", "grade": "גן", "gender": "female",
        "persona_en": (
            "You are ליבי, a 5-year-old Israeli girl in kindergarten taking an "
            "English lesson with tutor KOKO. You barely know English - only a "
            "few single words (cat, dog, red, ball). Reply mostly in HEBREW, "
            "very short (1-4 words). Sometimes don't answer, ask something "
            "unrelated, or say 'לא יודעת'. When asked to repeat an English "
            "word, try - half the time it's garbled ('יללו' for yellow)."
        ),
        "persona_math": (
            "You are ליבי, a 5-year-old Israeli girl in kindergarten doing a "
            "math activity with tutor קוקו. You can count to 10 and do 1+1 "
            "type problems. Reply in very short Hebrew. Sometimes say 'לא "
            "יודעת' or count wrong."
        ),
        "max_en_ratio": 0.35, "min_english_replies": 0,
    },
    {
        "name": "לביא", "grade": "כיתה א", "gender": "male",
        "persona_en": (
            "You are לביא, a 6-year-old Israeli boy in 1st grade taking an "
            "English lesson with tutor KOKO. You know a few English words "
            "(car, dog, cat, ball). Reply in short Hebrew or single English "
            "words. IMPORTANT: when asked to say 'car', answer exactly 'קר' "
            "(speech-recognition garble). Other words: try, small mistakes ok."
        ),
        "persona_math": (
            "You are לביא, a 6-year-old Israeli boy in 1st grade doing math "
            "with tutor קוקו. You know addition up to 20, struggle with "
            "subtraction sometimes. Reply in short Hebrew with the number."
        ),
        "max_en_ratio": 0.45, "min_english_replies": 0,
    },
    {
        "name": "ינאי", "grade": "כיתה ג", "gender": "male",
        "persona_en": (
            "You are ינאי, an 8-year-old Israeli boy in 3rd grade taking an "
            "English lesson with tutor KOKO. You know basic English words and "
            "can repeat short phrases. Answer briefly, mostly Hebrew with "
            "English words when asked. Small mistakes sometimes."
        ),
        "persona_math": (
            "You are ינאי, an 8-year-old Israeli boy in 3rd grade doing math "
            "with tutor קוקו. You know multiplication tables up to 10 and "
            "two-digit addition. Reply in short Hebrew. Occasional slips."
        ),
        "max_en_ratio": 0.55, "min_english_replies": 1,
    },
    {
        "name": "נויה", "grade": "כיתה ה", "gender": "female",
        "persona_en": (
            "You are נויה, a 10-year-old Israeli girl in 5th grade taking an "
            "English lesson with tutor KOKO. You already know English well - "
            "full simple sentences. Answer in short English sentences when "
            "you can, Hebrew when unsure. Confident and engaged."
        ),
        "persona_math": (
            "You are נויה, a 10-year-old Israeli girl in 5th grade doing math "
            "with tutor קוקו. You handle multi-digit arithmetic and simple "
            "fractions. Reply in short Hebrew with the answer. Occasionally "
            "ask a smart question."
        ),
        "max_en_ratio": 0.85, "min_english_replies": 2,
    },
]

FEM_FORMS = re.compile(r"\b(בואי|תנסי|ספרי|אמרי|כתבי)\b|את (יכולה|מוכנה|אוהבת|מצליחה|לומדת)")
MASC_FORMS = re.compile(r"\b(בוא |תנסה |אתה |מוכן )\b")
HEBREW_RE = re.compile(r"[א-ת]")
LATIN_RE = re.compile(r"[A-Za-z]")
COLOR_RE = re.compile(r"\b(red|blue|yellow|green|orange|pink|purple|אדום|כחול|צהוב|ירוק|כתום|ורוד|סגול|צבע)\b", re.I)
EN_TEACH_RE = re.compile(r"'[a-z]+'\s*(זה|היא)|באנגלית (זה|היא|אומרים)", re.I)
# Steering back to math after an off-topic reply: one honest translation
# sentence is correct behavior (prompt rule 12); failing means the reply
# never returned to math.
MATH_RETURN_RE = re.compile(r"(נחזור|חזר[הו]? ל|עכשיו נמשיך|התרגיל|החישוב|כמה זה|כמה נשאר|בוא נ|בואי נ)")
TASK_RE = re.compile(r"[?!]$|(תנס|תגיד|אמור|בוא|תחזור|ענה|כתוב|נסה|חשב|כמה)")
# Any alphabet that is neither Hebrew nor Latin is a language-mix slip
# (seen in QA: a Russian "попробовать" inside a Hebrew sentence).
FOREIGN_ALPHA_RE = re.compile("[\\u0400-\\u04FF\\u0600-\\u06FF\\u0370-\\u03FF]+")

LAT = {"non_llm": [], "chat": [], "lesson": []}   # latency buckets (seconds)
NON_LLM_BUDGET, LLM_BUDGET = 5.0, 45.0


def en_ratio(t: str) -> float:
    he, la = len(HEBREW_RE.findall(t)), len(LATIN_RE.findall(t))
    return la / (he + la) if (he + la) else 0.0


def _arith_candidates(text: str) -> list:
    """All values a bare-number child reply could legitimately be answering.
    Simplified mirror of tutors._extract_correct_arithmetic_answer: symbolic
    expressions plus Hebrew worded operator pairs (sentence-scoped fallback).
    Question sentences are preferred so a worked-example number inside the
    reply does not mark a wrong answer as 'doubted'."""

    def _scan(t: str, out: list) -> None:
        for m in re.finditer(r"(\d+(?:\s*[\+\-\*/×x]\s*\d+)+)", t):
            e = m.group(1).replace("×", "*").replace("x", "*").replace("−", "-").replace(" ", "")
            if "**" in e:
                continue
            try:
                out.append(eval(e, {"__builtins__": {}}, {}))
            except Exception:
                pass
        nums = list(re.finditer(r"\d+", t))
        for i in range(len(nums) - 1):
            a, b = nums[i].group(), nums[i + 1].group()
            between = t[nums[i].end():nums[i + 1].start()].lower()
            if re.search(r"(\+|ועוד|פלוס|מוסיף|מצטרף|חיבור|סך)", between):
                op = "+"
            elif re.search(r"(-|פחות|מינוס|נשאר|הוריד|לוקח|מוריד|מחסיר|עוזב|איבד|אוכל)", between):
                op = "-"
            elif re.search(r"(\*|כפול|כפל|פעמים)", between):
                op = "*"
            elif re.search(r"(/|חלקי|חלק|לחלק)", between):
                op = "/"
            else:
                seg_start = max(t.rfind(".", 0, nums[i].start()),
                                t.rfind("?", 0, nums[i].start()),
                                t.rfind("!", 0, nums[i].start())) + 1
                seg_ends = [p for p in (t.find(".", nums[i + 1].end()),
                                        t.find("?", nums[i + 1].end()),
                                        t.find("!", nums[i + 1].end())) if p != -1]
                seg_end = min(seg_ends) if seg_ends else len(t)
                seg = t[seg_start:seg_end].lower()
                if re.search(r"(פחות|מינוס|מחסיר|מוריד|לוקח|נשאר|עוזב|איבד|חיסור)", seg):
                    op = "-"
                elif re.search(r"(ועוד|פלוס|מוסיף|חיבור|מחבר)", seg):
                    op = "+"
                elif re.search(r"(כפל|כפול|פעמים)", seg):
                    op = "*"
                elif re.search(r"(חלק|לחלק|חילוק)", seg):
                    op = "/"
                else:
                    continue
            try:
                out.append(eval(f"{a} {op} {b}", {"__builtins__": {}}, {}))
            except Exception:
                pass

    question_vals = []
    for m in re.finditer(r"[^.\n?!]*(?:כמה|\?)[^.\n?!]*[.?!]?", text):
        _scan(m.group(), question_vals)
    if question_vals:
        return question_vals
    vals = []
    _scan(text, vals)
    return vals


_REPLY_EQ_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
    r"([-+*/×x:÷−]|פחות|מינוס|ועוד|פלוס|כפול|פעמים|חלקי|לחלק)\s*"
    r"(\d+(?:\.\d+)?)\s*(?:=|שווה|זה|יוצא)\s*(\d+(?:\.\d+)?)(?!\d)")


def _wrong_reply_equations(text: str) -> list:
    """Equations KOKO stated in her own reply whose right side is wrong.
    Mirror of tutors._sanitize_reply_arithmetic - a hit means the sanitizer
    let a wrong equation reach the child (production bug: '6 - 2 = 3')."""
    _OPS = {"פחות": "-", "מינוס": "-", "ועוד": "+", "פלוס": "+",
            "כפול": "*", "פעמים": "*", "חלקי": "/", "לחלק": "/",
            "×": "*", "x": "*", "÷": "/", ":": "/", "−": "-"}
    wrong = []
    for m in _REPLY_EQ_RE.finditer(text):
        # Mid-chain (2+3+4=9): skip - the '=' applies to the whole chain.
        if re.search(r"[+\-*/×x:÷−]\s*$", text[:m.start()]):
            continue
        a, op, b, res = m.groups()
        sym = _OPS.get(op.strip(), op.strip())
        expr = f"{a} {sym} {b}"
        if "**" in expr:
            continue
        try:
            val = eval(expr, {"__builtins__": {}}, {})
        except Exception:
            continue
        if isinstance(val, complex) or abs(val - float(res)) > 1e-9:
            wrong.append((m.group(0), val, res))
    return wrong


async def timed(client, method, url, bucket="non_llm", **kw):
    t0 = time.monotonic()
    r = await client.request(method, url, **kw)
    LAT[bucket].append(time.monotonic() - t0)
    return r


async def child_reply(persona: str, history: list, last_msg: str) -> str:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    conv = "\n".join(
        f"{'TUTOR' if m['role'] == 'assistant' else 'YOU'}: {m['content']}"
        for m in history[-8:]
    )
    resp = await client.chat.completions.create(
        model="gpt-4o-mini", temperature=0.7, max_tokens=60,
        messages=[
            {"role": "system", "content": persona + "\nYou are the CHILD, never the tutor. Reply ONLY with your short answer as the child would say it - no praising yourself, no asking the next question, no quotes, no narration."},
            {"role": "user", "content": f"Conversation:\n{conv}\nTUTOR just said: {last_msg}\nYour reply:"},
        ],
    )
    return resp.choices[0].message.content.strip()


async def ensure_user(client, kid):
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.name == kid["name"]))).scalar_one_or_none()
        if user is None:
            user = User(name=kid["name"], role="child", grade_level=kid["grade"])
            db.add(user); await db.commit(); await db.refresh(user)
        return user


ALL_NAMES = [k["name"] for k in KIDS]


def shared_asserts(kid, replies, messages):
    """Checks common to both subjects. Returns failure strings."""
    fails = []
    if not HEBREW_RE.search(replies[0]):
        fails.append(f"opening not in Hebrew: {replies[0][:80]!r}")
    wrong = FEM_FORMS if kid["gender"] == "male" else MASC_FORMS
    bad = [i for i, r in enumerate(replies) if wrong.search(r)]
    if bad:
        fails.append(f"wrong-gender address in turns {bad}")
    # Cross-child identity leak: KOKO must never address/reference another
    # child (production bug: a child's session/memory bled into a sibling's).
    leaked = [n for n in ALL_NAMES if n != kid["name"] and n in " ".join(replies)]
    if leaked:
        fails.append(f"other children's names leaked into replies: {leaked}")
    # Every turn should end with a task; tolerate a single LLM slip but flag
    # repeated passivity (the production bug: KOKO stalled after praise).
    # A question followed by a short hint ("איך אומרים X? רמז: מתחיל ב-G.")
    # still counts - the task is the question, the hint just trails it.
    def _has_task(r):
        return bool(TASK_RE.search(r.strip()) or re.search(r"[?!]", r[-90:]))
    no_task = [i for i, r in enumerate(replies[:-1]) if not _has_task(r)]
    if len(no_task) >= 2:
        fails.append(f"turns without a task/question: {no_task}")
    foreign = [(i, FOREIGN_ALPHA_RE.search(r).group()) for i, r in enumerate(replies) if FOREIGN_ALPHA_RE.search(r)]
    if foreign:
        fails.append(f"foreign-language words leaked in turns {foreign}")
    return fails


# ---------------------------------------------------------------------------
# Section: smoke — endpoint health, auth, latency budgets. No LLM calls.
# ---------------------------------------------------------------------------
async def section_smoke(client, kids, verbose) -> list[str]:
    fails = []
    pin = os.environ.get("APP_PIN", "")
    checks = [
        ("GET", "/api/ping", {"x-app-pin": pin}, 200),
        ("GET", "/api/health", {}, 200),
        ("GET", "/api/control/agents", {}, 401),                     # no PIN
        ("GET", "/api/control/agents", {"x-app-pin": pin}, 200),     # with PIN
        ("GET", "/api/tutor/english/courses", {"x-app-pin": pin}, 200),
        ("GET", "/api/tutor/nonexistent-endpoint", {"x-app-pin": pin}, 404),
    ]
    for method, url, hdrs, want in checks:
        r = await timed(client, method, url, headers=hdrs)
        if r.status_code != want:
            fails.append(f"{url}: {r.status_code} != {want}")
        elif verbose:
            print(f"  ok {method} {url} -> {r.status_code}")

    kid = kids[0]
    user = await ensure_user(client, kid)
    for url in [f"/api/tutor/progress/{user.id}",
                f"/api/tutor/english/placement/{user.id}"]:
        r = await timed(client, "GET", url, headers={"x-app-pin": pin})
        if r.status_code not in (200, 404):
            fails.append(f"{url}: {r.status_code}")
        elif verbose:
            print(f"  ok {url} -> {r.status_code}")

    for label, vals, budget in [("non-LLM", LAT["non_llm"], NON_LLM_BUDGET)]:
        for v in vals:
            if v > budget:
                fails.append(f"{label} latency {v:.1f}s > {budget}s")
    return fails


# ---------------------------------------------------------------------------
# Section: chat — English free-chat per kid (live LLM)
# ---------------------------------------------------------------------------
async def simulate_chat(client, kid, turns, verbose, subject="english") -> list[str]:
    fails, messages, replies = [], [], []
    user = await ensure_user(client, kid)
    pin = os.environ.get("APP_PIN", "")
    persona = kid["persona_en"] if subject == "english" else kid["persona_math"]
    url = "/api/tutor/english" if subject == "english" else "/api/tutor/chat"

    probe_reply, visual_reply, correct_probe_reply, wrong_probe_reply = None, None, None, None
    for i in range(turns):
        if i == 0:
            body_msgs = []
        else:
            # Scripted probes: off-topic question in math must steer back
            # (never a fabricated lesson); visual-method question must get a
            # text diagram, never "אני לא יכולה לצייר" (נויה's real session).
            if subject == "math" and i == 2:
                utt = "איך אומרים חתול באנגלית?"
            elif subject == "math" and i == 4 and kid["name"] == "נויה":
                utt = "את יכולה להראות לי איך פותרים 23 כפול 15 בשיטת המלבן? כאילו בציור"
            elif subject == "math" and i == 6:
                # Correct answer - must not be doubted. נויה states the
                # result in Hebrew words (Whisper does this constantly;
                # production miss where "שלושים וחמש" got no confirmation).
                utt = ("456 פחות 213 זה מאתיים ארבעים ושלוש!"
                       if kid["name"] == "נויה" else "456 פחות 213 זה 243!")
            elif subject == "math" and i == 7:
                utt = "אני חושב ש-456 פחות 213 זה 250!"  # wrong - must not be praised
            else:
                utt = await child_reply(persona, messages, replies[-1])
            messages.append({"role": "user", "content": utt})
            body_msgs = list(messages)
        body = {"user_id": user.id, "messages": body_msgs, "idempotency_key": str(uuid.uuid4())}
        if subject != "english":
            body["subject"] = "math"
        r = await timed(client, "POST", url, "chat", json=body,
                        headers={"x-app-pin": pin}, timeout=60)
        if r.status_code != 200:
            fails.append(f"turn {i}: HTTP {r.status_code}: {r.text[:120]}")
            break
        reply = r.json()["reply"]
        if subject == "math" and i == 2:
            probe_reply = reply
        if subject == "math" and i == 4 and kid["name"] == "נויה":
            visual_reply = reply
        if subject == "math" and i == 6:
            correct_probe_reply = reply
        if subject == "math" and i == 7:
            wrong_probe_reply = reply
        messages.append({"role": "assistant", "content": reply})
        replies.append(reply)
        if verbose:
            shown = body_msgs[-1]["content"] if i else ""
            print(f"  [{i}] {kid['name']+': '+shown if shown else ''}\n      KOKO: {reply[:150]}")

    if not replies:
        return fails or ["no replies"]

    fails += shared_asserts(kid, replies, messages)

    if subject == "english":
        ratios = [en_ratio(r) for r in replies]
        if sum(ratios) / len(ratios) > kid["max_en_ratio"]:
            fails.append(f"too much English: mean {sum(ratios)/len(ratios):.2f} > {kid['max_en_ratio']}")
        n_en = sum(1 for r in replies if len(LATIN_RE.findall(r)) >= 8)
        if n_en < kid["min_english_replies"]:
            fails.append(f"too little English pushed: {n_en} < {kid['min_english_replies']}")
        # Colors as the OPENING topic is the banned default - only reply[0]
        # is truly unprompted; a child saying 'Pink!' at turn 1 makes the
        # follow-up colors legitimate (live QA: נויה drove a colors stretch
        # herself, then the lesson moved on to animals). A color
        # mini-activity mid-lesson is legitimate spiral review - it only
        # fails when colors dominate a strict majority of replies.
        opening_colors = len(COLOR_RE.findall(replies[0] if replies else ""))
        color_replies = sum(1 for r in replies if len(COLOR_RE.findall(r)) >= 2)
        dominated = color_replies > len(replies) // 2
        if opening_colors >= 2 or dominated:
            fails.append(
                f"colors-dominated lesson (default topic): opening={opening_colors} color_turns={color_replies}/{len(replies)} | "
                + " | ".join(r[:90] for r in replies[:2])
            )
        if kid["name"] == "לביא":  # the real-world קר/car regression
            rejected = 0
            for i, r in enumerate(replies[:-1]):
                if re.search(r"\bcar\b", r, re.I) and i + 1 < len(messages):
                    if "קר" in messages[i + 1].get("content", ""):
                        rejected += 1
            if rejected >= 3:
                fails.append(f"garbled 'קר'(car) rejected {rejected}x - must accept close attempts")
    else:  # math
        for i, r in enumerate(replies):
            if EN_TEACH_RE.search(r) and not MATH_RETURN_RE.search(r):
                fails.append(f"turn {i}: English teaching leaked into math without steering back: {r[:80]!r}")
                break
        ratios = [en_ratio(r) for r in replies]
        if sum(ratios) / len(ratios) > 0.30:
            fails.append("math replies contain too much English content")
        # Off-topic probe must be brief and steer back to math, not a
        # fabricated English lesson (production bug: "איך אומרים" -> made-up
        # 'ציור=drawing' answer, twice verbatim).
        if probe_reply is not None:
            if re.search(r"'[a-zA-Z]+'\s*(זה|היא|zeh)", probe_reply) and not re.search(
                r"(חשבון|תרגיל|מספר|כמה|בוא נחזור|חזור|נסה)", probe_reply
            ):
                fails.append(f"off-topic probe became a lesson instead of steering back: {probe_reply[:100]!r}")
        # Correct-answer probe: the child stated a correct answer (243) -
        # KOKO must confirm, never doubt (production bug: 456-213=243 was
        # called 'קרוב' and re-checked twice).
        if correct_probe_reply is not None and re.search(
            r"(קרוב|לא נכון|שגוי|נסה שוב|נסי שוב|אבל בוא|בוא נבדוק|בואי נבדוק|לא בדיוק|טעות)",
            correct_probe_reply,
        ):
            fails.append(f"doubted a correct answer (456-213=243): {correct_probe_reply[:100]!r}")
        # Wrong-answer probe: child stated 250 (real: 243) - KOKO must NOT
        # praise it as correct (production bug: 'כל הכבוד! התשובה היא אכן 4'
        # after the child said 5).
        if wrong_probe_reply is not None:
            praised = re.search(r"(כל הכבוד|נכון( מאוד)?|מעולה|מצוין|אלוף|יופי|מדויק)", wrong_probe_reply)
            corrected = re.search(r"(243|לא בדיוק|לא נכון|שגו|טעות|קרוב|בוא נבדוק|בואי נבדוק|נסה שוב|נסי שוב|כמעט)", wrong_probe_reply)
            if praised and not corrected:
                fails.append(f"praised a wrong answer (456-213=250): {wrong_probe_reply[:100]!r}")
        # Generic regression: a bare-number child reply that equals the
        # computed answer to KOKO's own question must never be doubted
        # (live QA catch: '15 מכוניות ומישהו לוקח 7' -> child '8' ->
        # KOKO said 'נסה שוב!' on a correct answer).
        for i in range(len(messages) - 2):
            a_msg, u_msg, n_msg = messages[i], messages[i + 1], messages[i + 2]
            if {a_msg.get("role"), u_msg.get("role"), n_msg.get("role")} != {"assistant", "user"} \
                    or a_msg.get("role") != "assistant" or u_msg.get("role") != "user":
                continue
            u_text = str(u_msg.get("content") or "").strip()
            if not re.fullmatch(r"\d{1,6}[.!]?", u_text):
                continue
            u_num = float(u_text.rstrip(".!"))
            if not any(abs(v - u_num) <= 1e-6 for v in _arith_candidates(str(a_msg.get("content") or ""))):
                continue
            n_text = str(n_msg.get("content") or "")
            if re.search(r"(נסה שוב|נסי שוב|בוא נבדוק|בואי נבדוק|לא בדיוק|לא נכון|שגוי|קרוב)", n_text):
                fails.append(
                    f"doubted a correct bare-number answer: {u_text!r} after {str(a_msg.get('content'))[:60]!r} "
                    f"-> {n_text[:80]!r}"
                )
                break
        # Generic regression: every equation KOKO states must be true -
        # '6 - 2 = 3' must never reach the child (production bug).
        for a_msg in messages:
            if a_msg.get("role") != "assistant":
                continue
            for bad, val, res in _wrong_reply_equations(str(a_msg.get("content") or "")):
                fails.append(f"KOKO stated a wrong equation {bad!r} (is {val}) in: {str(a_msg.get('content'))[:90]!r}")
                break
        # Visual-method probe: a drawing request must get a text table,
        # never a refusal (נויה's real session: 'אני לא יכולה לצייר כאן').
        if visual_reply is not None:
            if re.search(r"לא (יכולה|יכול|מסוגלת) ל(צייר|הראות|הציג)", visual_reply):
                fails.append(f"refused to draw: {visual_reply[:100]!r}")
            elif sum(1 for line in visual_reply.split("\n") if len(re.findall(r"\d", line)) >= 2) < 2:
                fails.append(f"no text diagram for visual method: {visual_reply[:120]!r}")
        # Praise-for-wrong-answer check: pure-number child reply that KOKO
        # both praises and contradicts (production bug: child said 5, KOKO
        # said 'כל הכבוד! התשובה היא אכן 4').
        for m, nxt in zip(messages, messages[1:]):
            if m["role"] == "user" and re.fullmatch(r"\s*\d+\s*", m.get("content", "")):
                said = int(m["content"].strip())
                if nxt["role"] == "assistant":
                    r = nxt["content"]
                    if re.search(r"התשובה היא (אכן |באמת )?(\d+)", r):
                        stated = int(re.search(r"התשובה היא (?:אכן |באמת )?(\d+)", r).group(1))
                        if stated != said and re.search(r"(כל הכבוד|נכון|מצוין|מעולה)", r):
                            fails.append(
                                f"praised wrong answer: child said {said}, KOKO praised but stated {stated}: {r[:100]!r}"
                            )

    return fails


# ---------------------------------------------------------------------------
# Section: lessons — structured lesson flow end-to-end
# ---------------------------------------------------------------------------
def _slide_answer(lesson_json, slide_id):
    """The real (unpublished) answer for a slide, from the lesson file."""
    for s in lesson_json["slides"]:
        if s["id"] == slide_id:
            if "accept" in s:
                return s["accept"][0]
            if "correct" in s:
                return s["correct"]
            if "answer" in s:
                return s["answer"]
            return s.get("target", "yes")
    return "yes"


async def section_lessons(client, kids, verbose) -> list[str]:
    fails = []
    pin = os.environ.get("APP_PIN", "")
    kid = kids[0]  # one kid is enough to validate the pipeline
    user = await ensure_user(client, kid)

    r = await timed(client, "GET", "/api/tutor/english/courses", headers={"x-app-pin": pin})
    courses = r.json().get("courses", [])
    if not courses:
        return ["no courses returned"]

    # Pick the first lesson that exists on disk and try the full flow.
    lesson_id, lesson_json = None, None
    for c in courses:
        for u in c.get("units", []):
            for lid in u.get("lessons", []):
                lid_s = lid if isinstance(lid, str) else lid.get("lesson_id") or lid.get("id")
                p = REPO / "lessons" / "english" / f"{lid_s}.json"
                if lid_s and p.exists():
                    lesson_id, lesson_json = lid_s, json.loads(p.read_text(encoding="utf-8"))
                    break
            if lesson_id:
                break
        if lesson_id:
            break
    if not lesson_id:
        return ["no on-disk lesson found in catalog"]

    r = await timed(client, "GET", f"/api/tutor/english/lesson/{lesson_id}?user_id={user.id}",
                    "lesson", headers={"x-app-pin": pin})
    if r.status_code in (403, 404):
        return [f"lesson {lesson_id} blocked: {r.status_code} (placement?) — investigate"]
    if r.status_code != 200:
        return [f"lesson fetch: {r.status_code}: {r.text[:120]}"]
    data = r.json()

    # Hebrew-first: every spoken slide must have a Hebrew instruction available
    for s in data["slides"]:
        if s.get("type") in ("speak", "listen") and not s.get("prompt_he"):
            fails.append(f"slide {s['id']}: spoken but no Hebrew prompt_he")

    correct = 0
    for s in data["slides"]:
        ans = _slide_answer(lesson_json, s["id"])
        r = await timed(client, "POST",
                        f"/api/tutor/english/lesson/{lesson_id}/answer?user_id={user.id}",
                        "lesson", headers={"x-app-pin": pin},
                        json={"slide_id": s["id"], "answer": ans}, timeout=30)
        if r.status_code != 200:
            fails.append(f"answer slide {s['id']}: {r.status_code}")
        else:
            res = r.json()
            if res.get("correct") is True:
                correct += 1
            elif res.get("correct") is False:
                fails.append(f"slide {s['id']}: correct answer {ans!r} marked wrong")

    r = await timed(client, "POST",
                    f"/api/tutor/english/lesson/{lesson_id}/complete?user_id={user.id}",
                    "lesson", headers={"x-app-pin": pin},
                    json={"stars_earned": correct}, timeout=30)
    if r.status_code == 200:
        res = r.json()
        if not res.get("completed"):
            fails.append("complete returned 200 but completed=False")
        elif verbose:
            print(f"  lesson completed: stars={res.get('stars_earned', res.get('earned', '?'))}")
    elif r.status_code == 409:
        if verbose:
            print(f"  complete returned 409 - treating as already recorded, OK")
    else:
        fails.append(f"lesson complete: {r.status_code}")

    # Rewards endpoint should respond sanely (200 or 404/409 - never 500)
    r = await timed(client, "POST", f"/api/tutor/english/rewards/claim/{user.id}",
                    headers={"x-app-pin": pin}, timeout=30)
    if r.status_code >= 500:
        fails.append(f"rewards claim: {r.status_code} server error")
    return fails


# ---------------------------------------------------------------------------
# Section: isolation — per-child history/identity separation
# ---------------------------------------------------------------------------
async def section_isolation(client, kids, verbose) -> list[str]:
    """A message sent as child A must appear only in A's history for the same
    subject - never in B's, never in A's other subject. Regression guard for
    the session/memory mixing bug where one child's log appeared in a
    sibling's session."""
    fails = []
    pin = os.environ.get("APP_PIN", "")
    kid_a, kid_b = kids[0], kids[-1]
    user_a = await ensure_user(client, kid_a)
    marker = f"מילת-סוד-ייחודית-{uuid.uuid4().hex[:8]}"

    r = await timed(client, "POST", "/api/tutor/chat", "chat",
                    headers={"x-app-pin": pin}, timeout=60,
                    json={"user_id": user_a.id, "subject": "math",
                          "messages": [{"role": "user", "content": marker}],
                          "idempotency_key": str(uuid.uuid4())})
    if r.status_code != 200:
        return [f"isolation probe turn failed: {r.status_code}"]

    async def history(name, subject):
        hr = await timed(client, "GET", f"/api/history/{name}?subject={subject}",
                         headers={"x-app-pin": pin}, timeout=30)
        if hr.status_code != 200:
            return None
        return " ".join(m.get("content") or "" for m in hr.json().get("messages", []))

    hist_a_math = await history(kid_a["name"], "math")
    hist_b_math = await history(kid_b["name"], "math")
    hist_a_en = await history(kid_a["name"], "english")

    if hist_a_math is None or marker not in hist_a_math:
        fails.append("marker missing from child A's own math history")
    if hist_b_math and marker in hist_b_math:
        fails.append(f"child A's message leaked into {kid_b['name']}'s math history")
    if hist_a_en and marker in hist_a_en:
        fails.append("math message leaked into A's english history (subject mixing)")
    if verbose and not fails:
        print(f"  marker {marker!r} isolated to {kid_a['name']}/math only")
    return fails


# ---------------------------------------------------------------------------
# Perf report
# ---------------------------------------------------------------------------
def perf_report() -> list[str]:
    lines, fails = [], []
    for bucket, vals in LAT.items():
        if not vals:
            continue
        budget = LLM_BUDGET if bucket == "chat" else NON_LLM_BUDGET
        p95 = statistics.quantiles(vals, n=20)[-1] if len(vals) > 1 else vals[0]
        lines.append(f"  {bucket:8s} n={len(vals):3d}  median={statistics.median(vals):5.1f}s  "
                     f"p95={p95:5.1f}s  max={max(vals):5.1f}s")
        for v in vals:
            if v > budget:
                fails.append(f"{bucket} latency {v:.1f}s > {budget}s")
    print("\n--- latency ---")
    print("\n".join(lines) if lines else "  (no requests)")
    return fails


async def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", default="all",
                    choices=["all", "smoke", "chat", "math", "lessons", "isolation"])
    ap.add_argument("--kids", help="comma-separated names (default: all)")
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        if Path(".env").exists():
            for line in open(".env", encoding="utf-8"):
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k, v)

    need_llm = args.section in ("all", "chat", "math", "isolation")
    if need_llm and not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY required for live sections.")
        return 2

    kids = KIDS
    if args.kids:
        wanted = set(args.kids.split(","))
        kids = [k for k in KIDS if k["name"] in wanted]

    total = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://sim") as client:
        if args.section in ("all", "smoke"):
            print("\n=== SMOKE: endpoints/auth/latency ===")
            fs = await section_smoke(client, kids, args.verbose)
            total += len(fs); [print(f"  FAIL: {f}") for f in fs]
            if not fs: print("  PASS")

        if args.section in ("all", "lessons"):
            print("\n=== LESSONS: structured flow ===")
            fs = await section_lessons(client, kids, args.verbose)
            total += len(fs); [print(f"  FAIL: {f}") for f in fs]
            if not fs: print("  PASS")

        if args.section in ("all", "isolation"):
            print("\n=== ISOLATION: per-child history/identity ===")
            fs = await section_isolation(client, kids, args.verbose)
            total += len(fs); [print(f"  FAIL: {f}") for f in fs]
            if not fs: print("  PASS")

        for subject, label in (("english", "ENGLISH CHAT"), ("math", "MATH CHAT")):
            if args.section not in ("all", "chat", "math"):
                continue
            if args.section == "chat" and subject == "math":
                continue
            if args.section == "math" and subject == "english":
                continue
            for kid in kids:
                print(f"\n=== {label}: {kid['name']} ({kid['grade']}) — {args.turns} turns ===")
                fs = await simulate_chat(client, kid, args.turns, args.verbose, subject)
                total += len(fs)
                if fs:
                    [print(f"  FAIL: {f}") for f in fs]
                else:
                    print("  PASS")

    total += len(perf_report())
    print(f"\n{'='*50}\n{'ALL PASS' if total == 0 else f'{total} FAILURES'}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
