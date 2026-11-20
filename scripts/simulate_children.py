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
EN_TEACH_RE = re.compile(r"'[a-z]+'\s*זה|באנגלית זה", re.I)
TASK_RE = re.compile(r"[?!]$|(תנס|תגיד|אמור|בוא|תחזור|ענה|כתוב|נסה|חשב|כמה)")

LAT = {"non_llm": [], "chat": [], "lesson": []}   # latency buckets (seconds)
NON_LLM_BUDGET, LLM_BUDGET = 5.0, 45.0


def en_ratio(t: str) -> float:
    he, la = len(HEBREW_RE.findall(t)), len(LATIN_RE.findall(t))
    return la / (he + la) if (he + la) else 0.0


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


def shared_asserts(kid, replies, messages):
    """Checks common to both subjects. Returns failure strings."""
    fails = []
    if not HEBREW_RE.search(replies[0]):
        fails.append(f"opening not in Hebrew: {replies[0][:80]!r}")
    wrong = FEM_FORMS if kid["gender"] == "male" else MASC_FORMS
    bad = [i for i, r in enumerate(replies) if wrong.search(r)]
    if bad:
        fails.append(f"wrong-gender address in turns {bad}")
    no_task = [i for i, r in enumerate(replies[:-1]) if not TASK_RE.search(r.strip())]
    if no_task:
        fails.append(f"turns without a task/question: {no_task}")
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

    for i in range(turns):
        if i == 0:
            body_msgs = []
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
        if len(COLOR_RE.findall(" ".join(replies[:2]))) >= 3:
            fails.append("colors-heavy opening (default topic)")
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
            if EN_TEACH_RE.search(r):
                fails.append(f"turn {i}: English teaching leaked into math: {r[:80]!r}")
                break
        ratios = [en_ratio(r) for r in replies]
        if sum(ratios) / len(ratios) > 0.30:
            fails.append("math replies contain too much English content")

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
            print(f"  daily limit hit (409) - completion already recorded, OK")
    else:
        fails.append(f"lesson complete: {r.status_code}")

    # Rewards endpoint should respond sanely (200 or 404/409 - never 500)
    r = await timed(client, "POST", f"/api/tutor/english/rewards/claim/{user.id}",
                    headers={"x-app-pin": pin}, timeout=30)
    if r.status_code >= 500:
        fails.append(f"rewards claim: {r.status_code} server error")
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
                    choices=["all", "smoke", "chat", "math", "lessons"])
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

    need_llm = args.section in ("all", "chat", "math")
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
