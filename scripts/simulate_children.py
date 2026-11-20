"""Child-simulation harness: role-plays each of the 4 kids against the real
KOKO English tutor (real OpenAI calls through the real /api/tutor/english
endpoint) and asserts level-appropriate, gender-consistent, Hebrew-first
behavior. Run before every deploy:

    python scripts/simulate_children.py            # all kids, 10 turns each
    python scripts/simulate_children.py --kids ליבי --turns 6
    python scripts/simulate_children.py --verbose  # print full transcripts

Requires OPENAI_API_KEY and APP_PIN in .env (or env vars). Exits 1 on failure.
Costs ~$0.02-0.05 per full run (gpt-4o-mini for KOKO + child simulator).
"""

import argparse
import asyncio
import os
import re
import sys
import uuid

import httpx
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal, User  # noqa: E402
from main import app  # noqa: E402

# ---------------------------------------------------------------------------
# The 4 kids: persona fed to the child-simulator LLM + expected KOKO behavior
# ---------------------------------------------------------------------------
KIDS = [
    {
        "name": "ליבי",
        "grade": "גן",
        "gender": "female",
        "persona": (
            "You are ליבי, a 5-year-old Israeli girl in kindergarten taking an "
            "English lesson with tutor KOKO. You barely know English - only a "
            "few single words (cat, dog, red, ball). You reply mostly in "
            "HEBREW, very short (1-4 words), like a small child. Sometimes you "
            "don't answer the question, ask something unrelated, or say 'לא "
            "יודעת'. When KOKO asks you to repeat an English word, try it - "
            "half the time your pronunciation comes out garbled (e.g. 'יללו' "
            "for yellow, 'קיט' for kid)."
        ),
        "max_en_ratio": 0.35,     # kindergarten: replies should be mostly Hebrew
        "min_english_replies": 0, # she shouldn't push full English sentences
    },
    {
        "name": "לביא",
        "grade": "כיתה א",
        "gender": "male",
        "persona": (
            "You are לביא, a 6-year-old Israeli boy in 1st grade taking an "
            "English lesson with tutor KOKO. You know a handful of English "
            "words (car, dog, cat, ball). You reply in short Hebrew or single "
            "English words. IMPORTANT: when KOKO asks you to say the word "
            "'car', answer exactly 'קר' (speech-recognition garble). For other "
            "words, try your best with occasional small mistakes."
        ),
        "max_en_ratio": 0.45,
        "min_english_replies": 0,
    },
    {
        "name": "ינאי",
        "grade": "כיתה ג",
        "gender": "male",
        "persona": (
            "You are ינאי, an 8-year-old Israeli boy in 3rd grade taking an "
            "English lesson with tutor KOKO. You know basic English words and "
            "can repeat short phrases. You answer briefly, mostly in Hebrew "
            "with English words when asked. You make small mistakes sometimes."
        ),
        "max_en_ratio": 0.55,
        "min_english_replies": 1,
    },
    {
        "name": "נויה",
        "grade": "כיתה ה",
        "gender": "female",
        "persona": (
            "You are נויה, a 10-year-old Israeli girl in 5th grade taking an "
            "English lesson with tutor KOKO. You already know English quite "
            "well - you can understand and produce full simple sentences. "
            "Answer in short English sentences when you can, Hebrew when "
            "unsure. You are confident and engaged."
        ),
        "max_en_ratio": 0.85,
        "min_english_replies": 2,  # she should get pushed toward real English
    },
]

FEM_FORMS = re.compile(r"\b(בואי|תנסי|ספרי|אמור|אמרי|כתבי)\b|את (יכולה|מוכנה|אוהבת|מצליחה|לומדת)")
MASC_FORMS = re.compile(r"\b(בוא |תנסה |אתה |מוכן |אוהד )\b")
HEBREW_RE = re.compile(r"[א-ת]")
LATIN_RE = re.compile(r"[A-Za-z]")
COLOR_RE = re.compile(r"\b(red|blue|yellow|green|orange|pink|purple|אדום|כחול|צהוב|ירוק|כתום|ורוד|סגול|צבע)\b", re.I)


def en_ratio(text: str) -> float:
    he = len(HEBREW_RE.findall(text))
    la = len(LATIN_RE.findall(text))
    return la / (he + la) if (he + la) else 0.0


async def child_reply(persona: str, history: list, koko_msg: str) -> str:
    """Generate the child's next utterance via a simulator LLM."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    conv = "\n".join(
        f"{'KOKO' if m['role'] == 'assistant' else 'YOU'}: {m['content']}"
        for m in history[-8:]
    )
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.7,
        max_tokens=60,
        messages=[
            {"role": "system", "content": persona + "\nReply ONLY with the child's exact utterance - no quotes, no narration."},
            {"role": "user", "content": f"Conversation so far:\n{conv}\nKOKO just said: {koko_msg}\nYour reply:"},
        ],
    )
    return resp.choices[0].message.content.strip()


async def ensure_user(kid: dict):
    async with SessionLocal() as db:
        res = await db.execute(select(User).where(User.name == kid["name"]))
        user = res.scalar_one_or_none()
        if user is None:
            user = User(name=kid["name"], role="child", grade_level=kid["grade"])
            db.add(user)
            await db.commit()
            await db.refresh(user)
        return user


async def simulate_kid(kid: dict, turns: int, verbose: bool) -> list[str]:
    """Run one full simulated session. Returns list of failures."""
    failures = []
    messages = []
    koko_replies = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sim"
    ) as client:
        user = await ensure_user(kid)
        pin = os.environ.get("APP_PIN", "")
        headers = {"x-app-pin": pin}

        for i in range(turns):
            if i == 0:
                user_msg = "__INIT__"  # fresh greeting: empty history
                body_msgs = []
            else:
                user_msg = await child_reply(kid["persona"], messages, koko_replies[-1])
                messages.append({"role": "user", "content": user_msg})
                body_msgs = messages

            r = await client.post(
                "/api/tutor/english",
                json={
                    "user_id": user.id,
                    "messages": body_msgs,
                    "idempotency_key": str(uuid.uuid4()),
                },
                headers=headers,
                timeout=60,
            )
            if r.status_code != 200:
                failures.append(f"turn {i}: HTTP {r.status_code}: {r.text[:120]}")
                break
            reply = r.json()["reply"]
            messages.append({"role": "assistant", "content": reply})
            koko_replies.append(reply)
            if verbose:
                who = "KOKO" if i == 0 else f"{kid['name']}"
                print(f"  [{i}] {who}: {user_msg if i else '(init)'}\n      KOKO: {reply[:160]}")

    if not koko_replies:
        return failures or ["no replies"]

    # --- Assertion 1: Hebrew-first opening -------------------------------
    first = koko_replies[0]
    if not HEBREW_RE.search(first):
        failures.append(f"opening not in Hebrew: {first[:80]!r}")

    # --- Assertion 2: gender-consistent address ---------------------------
    wrong = FEM_FORMS if kid["gender"] == "male" else MASC_FORMS
    bad_turns = [i for i, r in enumerate(koko_replies) if wrong.search(r)]
    if bad_turns:
        failures.append(
            f"wrong-gender address in turns {bad_turns}: "
            f"{[m.group(0) for i in bad_turns for m in wrong.finditer(koko_replies[i])][:4]}"
        )

    # --- Assertion 3: immersion level matches demonstrated ability --------
    ratios = [en_ratio(r) for r in koko_replies]
    mean_ratio = sum(ratios) / len(ratios)
    if mean_ratio > kid["max_en_ratio"]:
        failures.append(
            f"too much English for level: mean ratio {mean_ratio:.2f} > {kid['max_en_ratio']}"
        )
    n_english_replies = sum(1 for r in koko_replies if len(LATIN_RE.findall(r)) >= 8)
    if n_english_replies < kid["min_english_replies"]:
        failures.append(
            f"not enough English pushed: {n_english_replies} English-heavy replies "
            f"< {kid['min_english_replies']} expected"
        )

    # --- Assertion 4: no colors-by-default opening ------------------------
    early = " ".join(koko_replies[:2])
    if len(COLOR_RE.findall(early)) >= 3:
        failures.append(f"colors-heavy opening (default topic): {early[:100]!r}")

    # --- Assertion 5: every turn ends with a task/question ----------------
    no_task = [
        i for i, r in enumerate(koko_replies[:-1])
        if not re.search(r"[?!]|\?$", r.strip()) and not re.search(
            r"(תנס|תגיד|אמור|בוא|תחזור|ענה|כתוב|נסה)", r
        )
    ]
    if no_task:
        failures.append(f"turns ending without a task/question: {no_task}")

    # --- Assertion 6 (לביא): garbled 'car' handled ------------------------
    if kid["name"] == "לביא":
        car_asks = sum(
            1 for i, r in enumerate(koko_replies)
            if re.search(r"\bcar\b", r, re.I)
            and i > 0
            and messages and "קר" in (messages[-1].get("content") or "")
        )
        repeat_asks = 0
        for i, r in enumerate(koko_replies):
            if re.search(r"\bcar\b", r, re.I) and i < len(messages) - 1:
                nxt = messages[i + 1]
                if nxt["role"] == "user" and "קר" in nxt.get("content", ""):
                    repeat_asks += 1
        if repeat_asks >= 3:
            failures.append(
                f"garbled 'קר' (='car') rejected {repeat_asks}x - should accept phonetically-close attempt"
            )

    return failures


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kids", help="comma-separated names (default: all)")
    parser.add_argument("--turns", type=int, default=10)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        for line in open(".env", encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY required (live calls).")
        return 2

    kids = KIDS
    if args.kids:
        wanted = set(args.kids.split(","))
        kids = [k for k in KIDS if k["name"] in wanted]

    total_fail = 0
    for kid in kids:
        print(f"\n=== {kid['name']} ({kid['grade']}, {kid['gender']}) — {args.turns} turns ===")
        failures = await simulate_kid(kid, args.turns, args.verbose)
        if failures:
            total_fail += len(failures)
            for f in failures:
                print(f"  FAIL: {f}")
        else:
            print("  PASS")

    print(f"\n{'='*50}\n{'ALL PASS' if total_fail == 0 else f'{total_fail} FAILURES'}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
