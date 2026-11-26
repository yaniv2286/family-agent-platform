"""Scout Ideas agent - collects investment ideas from social/media sources
the user subscribes to, deduplicates them, and sends periodic LLM digests
through the messenger agent.

Sources are configured purely through environment variables:

    SCOUT_TELEGRAM_API_ID / SCOUT_TELEGRAM_API_HASH / SCOUT_TELEGRAM_SESSION
        User-client credentials from my.telegram.org plus a StringSession
        produced once by scripts/scout_telegram_auth.py. Required because a
        bot can only read chats it was added to - a user client reads the
        channels the user actually subscribes to.
    SCOUT_TELEGRAM_CHANNELS
        Comma-separated channel usernames or IDs to scan.
    SCOUT_RSS_FEEDS
        Comma-separated RSS/Atom feed URLs (articles, newsletters).
    SCOUT_DISCORD_BOT_TOKEN / SCOUT_DISCORD_CHANNEL_IDS
        A regular guild bot token plus channel snowflake IDs. Only works on
        servers where the bot was invited by an admin - there is no
        ToS-compliant way to read a personal Discord account.
    SCOUT_DIGEST_ENABLED / SCOUT_DIGEST_HOURS / SCOUT_DIGEST_MINUTE
        Scheduler configuration (see scheduler.py).
"""
import asyncio
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx
import json
import re
from fastapi import APIRouter
from loguru import logger
from sqlalchemy import func, select, update

from agent_mesh import agent_mesh
from database import SessionLocal, ScheduledReport, ScoutItem, ScoutSourceState

router = APIRouter(prefix="/api/scout", tags=["Scout"])

# Serialize scheduled and manual digest runs within this process.
_DIGEST_LOCK = asyncio.Lock()

MAX_ITEMS_PER_DIGEST = 60
MAX_CONTENT_CHARS = 1500
MAX_ENTRIES_PER_FEED = 30
MAX_MESSAGES_PER_CHANNEL = 200
MAX_TELEGRAM_MESSAGE_CHARS = 3900


def _csv_env(name: str) -> List[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


# ---------------------------------------------------------------------------
# Source state helpers (incremental cursors for telegram/discord)
# ---------------------------------------------------------------------------

async def _get_last_seen_id(source: str) -> Optional[str]:
    async with SessionLocal() as db:
        state = await db.get(ScoutSourceState, source)
        return state.last_seen_id if state else None


async def _update_cursors(cursors: Dict[str, str]) -> None:
    if not cursors:
        return
    async with SessionLocal() as db:
        for source, last_id in cursors.items():
            state = await db.get(ScoutSourceState, source)
            if state is None:
                state = ScoutSourceState(source=source)
                db.add(state)
            state.last_seen_id = last_id
            state.last_run_at = datetime.utcnow()
        await db.commit()


# ---------------------------------------------------------------------------
# Collectors. Each returns (items, cursors). A collector that is not
# configured simply returns ([], {}) so the platform runs fine without it.
# ---------------------------------------------------------------------------

async def _collect_telegram(channels: Optional[List[str]] = None) -> Tuple[List[dict], Dict[str, str]]:
    if channels is None:
        channels = _csv_env("SCOUT_TELEGRAM_CHANNELS")
    api_id = os.getenv("SCOUT_TELEGRAM_API_ID")
    api_hash = os.getenv("SCOUT_TELEGRAM_API_HASH")
    session = os.getenv("SCOUT_TELEGRAM_SESSION")
    if not (channels and api_id and api_hash and session):
        return [], {}
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        logger.warning("telethon is not installed; skipping Telegram collection")
        return [], {}

    items: List[dict] = []
    cursors: Dict[str, str] = {}
    async with TelegramClient(StringSession(session), int(api_id), api_hash) as client:
        # Populate the entity cache so bare channel IDs (channels without a
        # public @username) resolve through the user's dialogs.
        await client.get_dialogs()
        for chan in channels:
            source = f"telegram:{chan}"
            last_seen = await _get_last_seen_id(source)
            min_id = int(last_seen) if last_seen else 0
            max_id = min_id
            entity = int(chan) if chan.lstrip("-").isdigit() else chan
            try:
                resolved = await client.get_entity(entity)
                channel_title = getattr(resolved, "title", None) or chan
                async for msg in client.iter_messages(entity, min_id=min_id, limit=MAX_MESSAGES_PER_CHANNEL):
                    max_id = max(max_id, msg.id)
                    text = (msg.message or "").strip()
                    if not text:
                        continue
                    items.append({
                        "source": source,
                        "external_id": str(msg.id),
                        "author": channel_title,
                        "title": None,
                        "content": text[:MAX_CONTENT_CHARS],
                        "url": None,
                        "published_at": msg.date.replace(tzinfo=None) if msg.date else None,
                    })
            except Exception:
                logger.exception("Telegram collection failed for channel", channel=chan)
                continue
            if max_id > min_id:
                cursors[source] = str(max_id)
    return items, cursors


async def _collect_rss() -> Tuple[List[dict], Dict[str, str]]:
    feeds = _csv_env("SCOUT_RSS_FEEDS")
    if not feeds:
        return [], {}
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser is not installed; skipping RSS collection")
        return [], {}

    items: List[dict] = []
    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "ScoutIdeas/1.0 (+family-agent-platform)"},
    ) as client:
        for url in feeds:
            source = f"rss:{url}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except Exception:
                logger.exception("RSS fetch failed", feed=url)
                continue
            parsed = feedparser.parse(resp.content)
            feed_title = parsed.feed.get("title") if parsed.feed else None
            for entry in parsed.entries[:MAX_ENTRIES_PER_FEED]:
                ext_id = entry.get("id") or entry.get("link") or entry.get("title")
                if not ext_id:
                    continue
                published = None
                ts = entry.get("published_parsed") or entry.get("updated_parsed")
                if ts:
                    published = datetime(*ts[:6])
                items.append({
                    "source": source,
                    "external_id": str(ext_id)[:400],
                    "author": feed_title,
                    "title": (entry.get("title") or "")[:300] or None,
                    "content": (entry.get("summary") or entry.get("title") or "")[:MAX_CONTENT_CHARS],
                    "url": entry.get("link"),
                    "published_at": published,
                })
    return items, {}


async def _collect_discord() -> Tuple[List[dict], Dict[str, str]]:
    token = os.getenv("SCOUT_DISCORD_BOT_TOKEN")
    channel_ids = _csv_env("SCOUT_DISCORD_CHANNEL_IDS")
    if not (token and channel_ids):
        return [], {}
    try:
        import discord
    except ImportError:
        logger.warning("discord.py is not installed; skipping Discord collection")
        return [], {}

    items: List[dict] = []
    cursors: Dict[str, str] = {}
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    ready = asyncio.Event()

    @client.event
    async def on_ready():
        ready.set()

    start_task = asyncio.create_task(client.start(token))
    try:
        await asyncio.wait_for(ready.wait(), timeout=30)
        for cid in channel_ids:
            source = f"discord:{cid}"
            last_seen = await _get_last_seen_id(source)
            min_id = int(last_seen) if last_seen else 0
            max_id = min_id
            try:
                channel = client.get_channel(int(cid)) or await client.fetch_channel(int(cid))
                after = discord.Object(id=min_id) if min_id else None
                async for msg in channel.history(
                    limit=MAX_MESSAGES_PER_CHANNEL, after=after, oldest_first=True
                ):
                    max_id = max(max_id, msg.id)
                    text = (msg.content or "").strip()
                    if not text:
                        continue
                    items.append({
                        "source": source,
                        "external_id": str(msg.id),
                        "author": str(msg.author),
                        "title": None,
                        "content": text[:MAX_CONTENT_CHARS],
                        "url": msg.jump_url,
                        "published_at": msg.created_at.replace(tzinfo=None),
                    })
            except Exception:
                logger.exception("Discord collection failed for channel", channel_id=cid)
                continue
            if max_id > min_id:
                cursors[source] = str(max_id)
    finally:
        await client.close()
        await asyncio.gather(start_task, return_exceptions=True)
    return items, cursors


_COLLECTORS = [_collect_telegram, _collect_rss, _collect_discord]


def configured_sources() -> dict:
    """Which collectors are actually configured via env vars (for /status)."""
    telegram_ready = bool(
        os.getenv("SCOUT_TELEGRAM_API_ID")
        and os.getenv("SCOUT_TELEGRAM_API_HASH")
        and os.getenv("SCOUT_TELEGRAM_SESSION")
    )
    return {
        "telegram": {
            "enabled": telegram_ready,
            "channels": _csv_env("SCOUT_TELEGRAM_CHANNELS"),
        },
        "rss": {
            "enabled": bool(_csv_env("SCOUT_RSS_FEEDS")),
            "feeds": _csv_env("SCOUT_RSS_FEEDS"),
        },
        "discord": {
            "enabled": bool(os.getenv("SCOUT_DISCORD_BOT_TOKEN")),
            "channel_ids": _csv_env("SCOUT_DISCORD_CHANNEL_IDS"),
        },
    }


# ---------------------------------------------------------------------------
# Collection pipeline
# ---------------------------------------------------------------------------

async def collect_all(channels: Optional[List[str]] = None) -> int:
    """Run every collector, dedupe against scout_items, store new rows.

    `channels` optionally restricts the Telegram collector to a subset of the
    configured channel list (used by the on-demand bot command).

    Returns the number of new items stored.
    """
    collected: List[dict] = []
    cursors: Dict[str, str] = {}
    for collector in _COLLECTORS:
        try:
            if collector is _collect_telegram:
                items, cur = await collector(channels=channels)
            else:
                items, cur = await collector()
            collected.extend(items)
            cursors.update(cur)
        except Exception:
            logger.exception("Scout collector failed", collector=collector.__name__)

    if not collected:
        return 0

    new_count = 0
    async with SessionLocal() as db:
        for item in collected:
            exists = (await db.execute(
                select(ScoutItem.id).where(
                    ScoutItem.source == item["source"],
                    ScoutItem.external_id == item["external_id"],
                )
            )).scalar_one_or_none()
            if exists is not None:
                continue
            db.add(ScoutItem(
                source=item["source"],
                external_id=item["external_id"],
                author=item.get("author"),
                title=item.get("title"),
                content=item.get("content") or "",
                url=item.get("url"),
                published_at=item.get("published_at"),
            ))
            new_count += 1
        await db.commit()

    await _update_cursors(cursors)
    logger.info("Scout collection finished", collected=len(collected), new_items=new_count)
    return new_count


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

async def _summarize_items(items: List[dict]) -> str:
    """Turn collected items into a short Hebrew investment-ideas digest.

    Falls back to a deterministic list when the LLM is unavailable so a
    digest is never silently dropped.
    """
    if not items:
        return ""

    block_lines = []
    for i, it in enumerate(items, start=1):
        entry = f"[{i}] מקור: {it['source']}"
        if it.get("author"):
            entry += f" ({it['author']})"
        if it.get("title"):
            entry += f"\nכותרת: {it['title']}"
        entry += f"\n{it['content']}"
        if it.get("url"):
            entry += f"\nקישור: {it['url']}"
        block_lines.append(entry)
    block = "\n\n".join(block_lines)

    system_prompt = """אתה אנליסט השקעות בכיר שמכין תמצית קצרה בעברית עבור משקיע פרטי.
אתה מקבל אוסף הודעות ומאמרים ממקורות שהמשקיע מנוי אליהם (ערוצי טלגרם, פידי RSS, דיסקורד).

המשימה שלך - לחלץ רק רעיונות השקעה קונקרטיים ותובנות שוק בעלות ערך:

חובה מוחלטת - פורמט:
- שורה ראשונה: "💼 רעיונות השקעה מהרשת".
- לכל רעיון: בולט אחד בלבד בפורמט "• **טיקר/נכס** - כיוון (לונג/שורט/צפייה) - סיבה במשפט אחד. [מקור]"
- אם אין טיקר מפורש, כתוב את שם הנכס/המגמה.
- מקסימום 8 בולטים - בחר את המשמעותיים ביותר בלבד.
- אחרי הבולטים, שורת "🔎 נושאים חמים:" עם עד 3 נושאים שחזרו במקורות (גם בלי המלצה קונקרטית).
- אם אין שום רעיון או תוכן רלוונטי להשקעות, כתוב משפט אחד בלבד: "לא נמצאו רעיונות השקעה חדשים באוסף הזה."

חובה מוחלטת - תוכן:
- אל תמציא טיקרים או המלצות שלא מופיעות במקורות.
- התעלם מפרסומות, ספאם, תוכן קידום מכירות ופוסטים לא קשורים.
- אל תיתן ייעוץ השקעות משלך - רק תמצת מה שפורסם.
- תמציתי ביותר, אפס פלואף."""

    try:
        from tutors import call_llm  # local import avoids a circular import at module load time
        summary = await call_llm(
            system_prompt, [{"role": "user", "content": block}], max_tokens=1000
        )
        if summary and not summary.strip().startswith("מצטערים"):
            return summary.strip()
    except Exception:
        logger.exception("Scout digest LLM summarization failed; using fallback")

    return _build_fallback_digest(items)


def _build_fallback_digest(items: List[dict]) -> str:
    """Deterministic digest used only when the LLM call fails outright."""
    lines = ["💼 רעיונות השקעה מהרשת (תמצית גולמית - LLM לא זמין)"]
    for it in items[:10]:
        label = it.get("title") or (it.get("content") or "")[:80]
        lines.append(f"• {label} [{it['source']}]")
        if it.get("url"):
            lines.append(f"  {it['url']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deep-research pipeline (Gemini + Google Search grounding)
#
# Stage 1: _extract_candidates  - cheap LLM pass turns raw items into a
#           deduped candidate list: asset, direction, reason, source channels.
# Stage 2: _research_candidate  - Gemini with google_search tool researches
#           fundamentals, technicals, analyst ratings and recent reports,
#           then returns a verdict + numeric reasoning in Hebrew.
# Stage 3: _build_research_digest - formats verdict + source channel per idea.
# ---------------------------------------------------------------------------

_RESEARCH_VERDICTS = {"מומלץ": "✅", "זהירות": "⚠️", "לא מומלץ": "❌"}


def _display_source(source: str, author: Optional[str] = None) -> str:
    """'telegram:@chan' -> '@chan' (or the channel title when known)."""
    short = source.split(":", 1)[-1]
    return author or short


async def _extract_candidates(items: List[dict]) -> List[dict]:
    """LLM pass 1: extract concrete investment candidates from raw items.

    Returns a deduped list of dicts:
        {"asset", "direction", "reason", "sources": [channel names]}
    Same asset mentioned in several channels is merged into one candidate
    with all sources listed - repeated mentions are a signal, not a duplicate.
    """
    if not items:
        return []

    block_lines = []
    for i, it in enumerate(items, start=1):
        entry = f"[{i}] מקור: {_display_source(it['source'])}"
        if it.get("author"):
            entry += f" ({it['author']})"
        if it.get("title"):
            entry += f"\nכותרת: {it['title']}"
        entry += f"\n{it['content']}"
        block_lines.append(entry)
    block = "\n\n".join(block_lines)

    system_prompt = """אתה אנליסט השקעות. אתה מקבל אוסף הודעות ומאמרים ממקורות שהמשקיע מנוי אליהם.

המשימה: חלץ רק רעיונות השקעה קונקרטיים - נכס/טיקר/מניה ספציפית עם כיוון מוצע.

החזר JSON בלבד במבנה:
{"candidates": [{"asset": "שם נכס או טיקר", "direction": "long|short|watch", "reason": "סיבה במשפט אחד", "item_ids": [1, 3]}]}

חוקים:
- item_ids = מספרי הפריטים [i] שמהם נלקח הרעיון. אם אותו נכס מופיע בכמה פריטים - ציין את כולם.
- כלול רק נכסים ספציפיים (מניה/טיקר/ETF/קריפטו) - לא נושאים כלליים כמו "הנדל"ן" או "השקל".
- התעלם מפרסומות, ספאם וקידום מכירות.
- direction: long = המלצת קנייה, short = מכירה/שורט, watch = אזכור מעניין בלי המלצה ברורה.
- מקסימום 20 מועמדים, בסדר יורד של חשיבות.
- אם אין מועמדים - החזר {"candidates": []}."""

    try:
        from tutors import BaseAgent  # local import avoids circular imports
        raw = await BaseAgent()._call_llm(
            system_prompt,
            [{"role": "user", "content": block}],
            max_tokens=2000,
            primary_model="gpt-4o-mini",
            fallback_model="gpt-4o-mini",
            json_response=True,
        )
        data = json.loads(raw)
    except Exception:
        logger.exception("Candidate extraction failed")
        return []

    by_asset: Dict[str, dict] = {}
    for cand in (data.get("candidates") or []):
        asset = (cand.get("asset") or "").strip()
        if not asset:
            continue
        key = asset.lower()
        sources = by_asset.setdefault(key, {}).setdefault("sources", set())
        for iid in cand.get("item_ids") or []:
            try:
                it = items[int(iid) - 1]
                sources.add(_display_source(it["source"], it.get("author")))
            except (IndexError, ValueError):
                continue
        merged = by_asset.setdefault(
            key, {"asset": asset, "direction": "watch", "reason": "", "sources": set()}
        )
        merged["asset"] = asset
        merged["direction"] = cand.get("direction") or merged["direction"]
        merged["reason"] = cand.get("reason") or merged["reason"]

    candidates = list(by_asset.values())
    for cand in candidates:
        cand["sources"] = sorted(cand["sources"])
    candidates.sort(key=lambda c: -len(c["sources"]))
    return candidates


async def _research_candidate(candidate: dict) -> Optional[dict]:
    """Gemini + Google Search grounding: deep research on one candidate.

    Returns {"verdict", "reasoning", "sources"} or None when Gemini is not
    configured / fails (caller then falls back to the plain digest).
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    model = os.getenv("SCOUT_GEMINI_MODEL", "gemini-2.5-flash")

    direction_he = {"long": "קנייה/לונג", "short": "מכירה/שורט", "watch": "מעקב"}[
        candidate.get("direction", "watch")
    ]
    prompt = f"""חקור לעומק את הנכס "{candidate['asset']}" כהשקעה, ותן פסק דין.

רקע: ערוץ/מקור המליץ עליו בכיוון {direction_he}. נימוק המקור: {candidate.get('reason', 'לא צוין')}.

בצע חיפוש אינטרנטי אמיתי ובדוק:
1. פונדומנטלס עדכניים - מכירות, רווח נקי, מכפיל רווח, מגמת תוצאות ותחזית הנהלה.
2. תמונת טכני - מגמה, תמיכה/התנגדות, מומנטום.
3. דירוגי אנליסטים ויעדי מחיר עדכניים.
4. דוחות כספיים אחרונים וחדשות עדכניות שיכולות לשנות את התמונה.

החזר JSON בלבד (בלי markdown, בלי ```):
{{"verdict": "מומלץ|זהירות|לא מומלץ", "reasoning": "3-5 משפטים בעברית עם מספרים קונקרטיים (מכפיל, רווח, יעד מחיר, שינוי אחוזים) שמצדיקים את הפסיקה", "confidence": "גבוהה|בינונית|נמוכה"}}

חוקים: אל תמציא נתונים; אם אין מידע מספיק על הנכס - verdict="זהירות" וציין זאת; זו סקירת מידע בלבד, לא ייעוץ השקעות."""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        # Gemini 2.5 thinking tokens count against maxOutputTokens - keep the
        # budget generous so the JSON answer is never truncated mid-object.
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096},
    }
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, params={"key": api_key}, json=payload)
            resp.raise_for_status()
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        # Thinking models emit {"thought": true, "text": ...} parts - skip them.
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        # Grounding citations (search sources actually used)
        grounding = data["candidates"][0].get("groundingMetadata") or {}
        citations = [
            (c.get("web") or {}).get("uri")
            for c in grounding.get("groundingChunks", [])
        ]
    except Exception:
        logger.exception("Gemini research failed", asset=candidate.get("asset"))
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        # The model may prepend prose - take the outermost JSON object only.
        obj = re.search(r"\{.*\}", cleaned, re.S)
        research = json.loads(obj.group(0) if obj else cleaned)
    except json.JSONDecodeError:
        # Truncated or malformed JSON: salvage whatever fields did survive so
        # the digest still shows a clean verdict/reasoning instead of raw JSON.
        logger.warning("Gemini research not valid JSON; salvaging fields", asset=candidate.get("asset"))
        def _field(name: str) -> Optional[str]:
            m = re.search(rf'"{name}"\s*:\s*"((?:[^"\\]|\\.)*)', cleaned)
            return m.group(1).replace('\\"', '"').replace("\\n", " ") if m else None
        research = {
            "verdict": _field("verdict") or "זהירות",
            "reasoning": _field("reasoning") or cleaned[:600],
            "confidence": _field("confidence") or "נמוכה",
        }

    verdict = research.get("verdict") or "זהירות"
    if verdict not in _RESEARCH_VERDICTS:
        verdict = "זהירות"
    return {
        "verdict": verdict,
        "reasoning": (research.get("reasoning") or "")[:600],
        "confidence": research.get("confidence") or "",
        "citations": [u for u in citations if u][:3],
    }


def _build_research_digest(candidates: List[dict], max_ideas: int) -> List[str]:
    """Format the digest as one or more Telegram-sized messages.

    Each idea is an indivisible block; blocks are packed greedily so no
    message exceeds the Telegram limit and no idea is cut mid-block.
    """
    direction_he = {"long": "לונג", "short": "שורט", "watch": "מעקב"}
    blocks: List[str] = []
    for i, cand in enumerate(candidates[:max_ideas], start=1):
        research = cand.get("research") or {}
        verdict = research.get("verdict", "זהירות")
        emoji = _RESEARCH_VERDICTS.get(verdict, "⚠️")
        direction = direction_he.get(cand.get("direction", "watch"), "מעקב")
        lines = [f"{i}. **{cand['asset']}** — {emoji} {verdict} (הערוץ הציע: {direction})"]
        if cand.get("sources"):
            lines.append(f"   📌 נסרק מ: {', '.join(cand['sources'])}")
        if cand.get("reason"):
            lines.append(f"   הערוץ אומר: {cand['reason']}")
        if research.get("reasoning"):
            lines.append(f"   ניתוח: {research['reasoning']}")
        if research.get("confidence"):
            lines.append(f"   רמת ביטחון: {research['confidence']}")
        blocks.append("\n".join(lines))

    header = "💼 מחקר השקעות מעמיק - מבוסס מחקר אינטרנטי חי\n"
    footer = "⚖️ מידע בלבד - לא ייעוץ השקעות. בדוק בעצמך לפני פעולה."
    # Reserve room for the scope header prepended to the first chunk and the
    # disclaimer appended to the last.
    budget = MAX_TELEGRAM_MESSAGE_CHARS - len(header) - len(footer) - 120
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for block in blocks:
        if cur and cur_len + len(block) + 1 > budget:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(block)
        cur_len += len(block) + 1
    chunks.append("\n".join(cur))
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            chunk = f"(המשך {idx + 1}/{len(chunks)})\n{chunk}"
        chunks[idx] = chunk
    chunks[0] = header + chunks[0]
    chunks[-1] = chunks[-1] + f"\n\n{footer}"
    return chunks


async def _researched_summarize(items: List[dict]) -> Optional[List[str]]:
    """Full pipeline: extract candidates, deep-research top N, format digest.

    Returns a list of Telegram-sized message chunks, or None when the
    research path is unavailable (no GEMINI_API_KEY or extraction produced
    nothing) so the caller falls back to the plain digest.
    """
    if not os.getenv("GEMINI_API_KEY"):
        return None
    max_ideas = int(os.getenv("SCOUT_RESEARCH_MAX_IDEAS", "15"))

    candidates = await _extract_candidates(items)
    if not candidates:
        return None

    # Research calls run concurrently (bounded) - sequential Gemini calls at
    # ~20s each would turn a 15-idea digest into a five-minute wait.
    sem = asyncio.Semaphore(4)

    async def _one(cand: dict) -> None:
        async with sem:
            cand["research"] = await _research_candidate(cand)

    await asyncio.gather(*(_one(c) for c in candidates[:max_ideas]))

    return _build_research_digest(candidates, max_ideas)


async def _send_via_messenger(text: str, channel: str = "telegram") -> bool:
    """Ask the messenger agent to deliver text to a channel."""
    try:
        response = await agent_mesh.ask(
            to_agent="messenger",
            intent="send_message",
            user_id=0,
            child_id=0,
            source_agent="scout_ideas",
            payload={"channel": channel, "text": text},
            timeout=20.0,
        )
        ok = response.get("ok") and response.get("result", {}).get("sent") is True
        logger.info("Messenger send result for scout digest", ok=ok)
        return ok
    except Exception:
        logger.exception("Failed to ask messenger to send scout digest")
        return False


async def run_scout_digest(channels: Optional[List[str]] = None) -> dict:
    """Entry point: collect new items, summarize pending items, send digest.

    `channels` optionally restricts the Telegram collection to a subset of the
    configured channels (manual /scout command). The digest itself still draws
    from all pending fresh items, so a filtered run only affects what is
    newly collected.

    Items stay digested=0 when the Telegram send fails so the next run picks
    them up again instead of losing them silently.
    """
    async with _DIGEST_LOCK:
        logger.info("Starting scout digest run", channels=channels or "all")
        collected = await collect_all(channels=channels)

        pending_ids: List[int] = []
        pending_items: List[dict] = []
        async with SessionLocal() as db:
            # Stale gate: a "buy" call from days ago is not an idea anymore.
            # Mark old backlog as digested=2 (skipped-stale) so it never
            # reaches the digest; only fresh items continue.
            stale_hours = int(os.getenv("SCOUT_STALE_HOURS", "48"))
            cutoff = datetime.utcnow() - timedelta(hours=stale_hours)
            stale = await db.execute(
                update(ScoutItem)
                .where(
                    ScoutItem.digested == 0,
                    func.coalesce(ScoutItem.published_at, ScoutItem.collected_at) < cutoff,
                )
                .values(digested=2)
            )
            if stale.rowcount:
                logger.info("Marked stale scout items as skipped", count=stale.rowcount)

            # Fair sampling: fetch a window of fresh pending items and pick
            # round-robin per source so one hyper-active channel cannot
            # dominate the whole digest (first backfill bug).
            rows = (await db.execute(
                select(ScoutItem)
                .where(ScoutItem.digested == 0)
                .order_by(ScoutItem.id)
                .limit(MAX_ITEMS_PER_DIGEST * 10)
            )).scalars().all()
            by_source: Dict[str, list] = {}
            for row in rows:
                by_source.setdefault(row.source, []).append(row)
            picked = []
            while len(picked) < MAX_ITEMS_PER_DIGEST and any(by_source.values()):
                for src in list(by_source):
                    if by_source[src]:
                        picked.append(by_source[src].pop(0))
                        if len(picked) >= MAX_ITEMS_PER_DIGEST:
                            break

            # Capture plain dicts inside the session - reading ORM attributes
            # after commit triggers MissingGreenlet (see AGENTS.md).
            for row in picked:
                pending_ids.append(row.id)
                pending_items.append({
                    "source": row.source,
                    "author": row.author,
                    "title": row.title,
                    "content": row.content,
                    "url": row.url,
                })
            await db.commit()

        if not pending_items:
            logger.info("Scout digest: no pending items; skipping send")
            return {
                "status": "ok",
                "items_collected": collected,
                "items_digested": 0,
                "telegram_sent": False,
                "skipped": True,
                "summary_text": None,
            }

        # Prefer the deep-research pipeline (Gemini + live web search) when
        # configured; otherwise keep the plain LLM digest.
        summary_chunks = await _researched_summarize(pending_items)
        if summary_chunks is None:
            summary_chunks = [await _summarize_items(pending_items)]
        stamp = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
        if channels is None:
            scope_note = "כל הערוצים"
        else:
            titles = await _channel_titles()
            names = [titles.get(c) or c for c in channels[:4]]
            scope_note = ", ".join(names) + (
                f" ועוד {len(channels) - 4}" if len(channels) > 4 else ""
            )
        header = f"📈 Scout Ideas - {stamp} UTC\n🔎 סריקת: {scope_note}\n"
        digest_text = header + "\n".join(summary_chunks)
        sent = True
        for idx, chunk in enumerate(summary_chunks):
            msg = header + chunk if idx == 0 else chunk
            if not await _send_via_messenger(msg):
                sent = False
                break

        async with SessionLocal() as db:
            if sent:
                await db.execute(
                    update(ScoutItem).where(ScoutItem.id.in_(pending_ids)).values(digested=1)
                )
            db.add(ScheduledReport(
                report_type="investment_digest",
                content=digest_text,
                sent_at=datetime.utcnow() if sent else None,
            ))
            await db.commit()

        await agent_mesh.publish(
            "scout_ideas.digest_sent" if sent else "scout_ideas.digest_failed",
            user_id=0,
            child_id=0,
            source_agent="scout_ideas",
            payload={"items_digested": len(pending_ids), "telegram_sent": sent},
        )

        logger.info(
            "Scout digest run finished",
            items_digested=len(pending_ids),
            telegram_sent=sent,
        )
        return {
            "status": "ok",
            "items_collected": collected,
            "items_digested": len(pending_ids),
            "telegram_sent": sent,
            "skipped": False,
            "summary_text": digest_text,
        }


# ---------------------------------------------------------------------------
# Mesh handlers + HTTP endpoints
# ---------------------------------------------------------------------------

async def handle_collect_digest(event, payload):
    """Mesh ask handler: run a digest on demand."""
    channels = (payload or {}).get("channels")
    return await run_scout_digest(channels=channels)


agent_mesh.on_ask("scout_ideas", "collect_digest", handle_collect_digest)


# ---------------------------------------------------------------------------
# Telegram bot commands (/scout ...) - on-demand digest + channel selection
# ---------------------------------------------------------------------------

SCOUT_BOT_COMMANDS = [
    {"command": "scout", "description": "סריקה מיידית של כל הערוצים"},
    {"command": "scout_list", "description": "רשימת הערוצים הממוספרים"},
    {"command": "scout_help", "description": "עזרה בפקודות"},
]


def parse_scout_command(text: str) -> Tuple[str, Optional[List[str]]]:
    """Parse a /scout command into (action, channels).

    action: "run" | "list" | "help" | "unknown"
    channels: None = all configured channels, otherwise a subset chosen by
    channel number (from /scout_list) or @username/id.
    """
    parts = (text or "").strip().split(None, 1)
    cmd = parts[0].lower().lstrip("/") if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("scout_list", "scout_channels"):
        return "list", None
    if cmd in ("scout_help", "scouthelp"):
        return "help", None
    if cmd != "scout":
        return "unknown", None
    if not args:
        return "run", None

    configured = _csv_env("SCOUT_TELEGRAM_CHANNELS")
    channels: List[str] = []
    for tok in re.split(r"[,\s]+", args):
        if not tok:
            continue
        if tok.isdigit():
            idx = int(tok)
            if 1 <= idx <= len(configured):
                channels.append(configured[idx - 1])
        else:
            channels.append(tok if (tok.startswith("@") or tok.lstrip("-").isdigit()) else f"@{tok}")
    return "run", channels or None


async def _bot_reply(text: str, reply_markup: Optional[dict] = None) -> None:
    """Reply into the family Telegram chat via the messenger bot token."""
    from agents.messenger_agent import send_telegram_message  # avoid circulars
    await send_telegram_message(text, reply_markup=reply_markup)


def _channel_keyboard(channels: List[str], titles: Dict[str, str]) -> dict:
    """Inline keyboard: one row per channel (tap = scan it) + an 'all' row.

    Button text is the channel's real display name only - no index, no
    @handle. The internal index stays in callback_data 's:<index>', which the
    messenger agent forwards as '/scout <index>' back to this handler.
    """
    rows = [[{
        "text": "🌐 סריקת כל הערוצים",
        "callback_data": "s:all",
    }]]
    row: List[dict] = []
    seen_labels: Dict[str, int] = {}
    for i, chan in enumerate(channels, start=1):
        label = titles.get(chan) or chan
        # Two sources can share an identical Telegram title (e.g. a channel and
        # its discussion group) - keep their buttons distinguishable.
        if label in seen_labels:
            seen_labels[label] += 1
            label = f"{label} ({seen_labels[label]})"
        else:
            seen_labels[label] = 1
        row.append({"text": label[:40], "callback_data": f"s:{i}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


async def _channel_titles() -> Dict[str, str]:
    """{channel_handle: real channel title} resolved from the latest collected
    item per source (collector stores the resolved Telegram title in author).
    Falls back to the raw handle when nothing was collected yet."""
    titles: Dict[str, str] = {}
    try:
        async with SessionLocal() as db:
            latest_ids = select(func.max(ScoutItem.id)).group_by(ScoutItem.source).subquery()
            rows = (await db.execute(
                select(ScoutItem.source, ScoutItem.author)
                .where(ScoutItem.id.in_(select(latest_ids)))
            )).all()
            for source, author in rows:
                if author and source.startswith("telegram:"):
                    handle = source.split(":", 1)[1]
                    # Items collected before channel titles were stored keep
                    # the raw handle as author - that is NOT a resolved name.
                    if author != handle:
                        titles[handle] = author
    except Exception:
        logger.exception("Failed to resolve channel titles for /scout_list")
    return titles


async def _resolve_channel_titles(channels: List[str]) -> Dict[str, str]:
    """Channel titles as the user sees them in Telegram.

    Stored item authors cover channels that already produced items; for the
    rest (newly added / quiet channels) resolve live via the Telethon user
    session. Missing entries fall back to the raw handle.
    """
    titles = await _channel_titles()
    missing = [c for c in channels if c not in titles]
    if not missing:
        return titles
    api_id = os.getenv("SCOUT_TELEGRAM_API_ID")
    api_hash = os.getenv("SCOUT_TELEGRAM_API_HASH")
    session = os.getenv("SCOUT_TELEGRAM_SESSION")
    if not (api_id and api_hash and session):
        return titles
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        async with TelegramClient(StringSession(session), int(api_id), api_hash) as client:
            await client.get_dialogs()  # populate entity cache for bare IDs
            for chan in missing:
                try:
                    entity = int(chan) if chan.lstrip("-").isdigit() else chan
                    resolved = await client.get_entity(entity)
                    title = getattr(resolved, "title", None) or getattr(resolved, "first_name", None)
                    if title:
                        titles[chan] = title
                except Exception:
                    logger.warning("Could not resolve channel title", channel=chan)
    except Exception:
        logger.exception("Live channel title resolution failed")
    return titles


_COMMAND_HELP = (
    "🔎 פקודות Scout:\n"
    "/scout — סריקה מיידית של כל הערוצים\n"
    "/scout_list — רשימת הערוצים הממוספרים\n"
    "/scout 3 7 — סריקה רק של ערוצים 3 ו-7 מהרשימה\n"
    "/scout @Noaminvest — סריקה של ערוץ ספציפי לפי שם\n"
    "\nהדידג'סט האוטומטי רץ גם כך פעמיים ביום (08:15 ו-20:15)."
)


async def handle_telegram_command(event, payload):
    """Mesh handler: messenger forwards /scout commands here."""
    text = (payload or {}).get("text") or ""
    action, channels = parse_scout_command(text)
    logger.info("Scout telegram command", text=text[:60], action=action, channels=channels)

    if action in ("help", "unknown"):
        await _bot_reply(_COMMAND_HELP)
        return {"handled": True, "action": "help"}

    if action == "list":
        configured = _csv_env("SCOUT_TELEGRAM_CHANNELS")
        if not configured:
            await _bot_reply("לא מוגדרים ערוצי טלגרם (SCOUT_TELEGRAM_CHANNELS ריק).")
        else:
            titles = await _resolve_channel_titles(configured)
            listing = "\n".join(
                f"{i}. {titles.get(c) or c}"
                for i, c in enumerate(configured, start=1)
            )
            await _bot_reply(
                f"📡 {len(configured)} ערוצים מוגדרים — לחץ כפתור לסריקה מיידית:\n{listing}",
                reply_markup=_channel_keyboard(configured, titles),
            )
        return {"handled": True, "action": "list"}

    if _DIGEST_LOCK.locked():
        await _bot_reply("⏳ סריקה כבר רצה כרגע - הדידג'סט ישלח בסיומה.")
        return {"handled": True, "action": "busy"}

    if channels is None:
        scope = "כל הערוצים"
    else:
        titles = await _resolve_channel_titles(channels)
        names = [titles.get(c) or c for c in channels[:3]]
        scope = ", ".join(names) + (f" ועוד {len(channels) - 3}" if len(channels) > 3 else "")
    await _bot_reply(
        f"🔎 מתחיל סריקה יזומה — {scope}…\n"
        "(איסוף הודעות + מחקר Gemini חי - בערך 2-4 דקות)"
    )

    async def _run_and_report():
        result = await run_scout_digest(channels=channels)
        if result.get("skipped"):
            await _bot_reply("ℹ️ לא נמצאו פריטים חדשים מאז הסריקה האחרונה.")
        elif not result.get("telegram_sent"):
            await _bot_reply("⚠️ הסריקה הסתיימה אבל שליחת הדידג'סט נכשלה - נסה שוב בעוד דקה.")

    asyncio.create_task(_run_and_report())
    return {"handled": True, "action": "run"}


agent_mesh.on_ask("scout_ideas", "telegram_command", handle_telegram_command)


@router.post("/run-now")
async def scout_run_now(payload: Optional[dict] = None):
    """Manually trigger a collect + digest run.

    Optional JSON body: {"channels": ["@chan", "12345"]} restricts the
    Telegram collection to a subset (same as `/scout 3 7` in the bot).
    """
    channels = (payload or {}).get("channels") or None
    try:
        return await run_scout_digest(channels=channels)
    except Exception as e:
        logger.exception("Manual scout digest run failed")
        return {"status": "error", "message": str(e)}


@router.get("/items")
async def list_items(limit: int = 50, pending_only: bool = False):
    """Inspect recently collected items (newest first)."""
    async with SessionLocal() as db:
        q = select(ScoutItem).order_by(ScoutItem.id.desc()).limit(min(limit, 200))
        if pending_only:
            q = q.where(ScoutItem.digested == 0)
        rows = (await db.execute(q)).scalars().all()
        return {
            "items": [
                {
                    "id": row.id,
                    "source": row.source,
                    "external_id": row.external_id,
                    "author": row.author,
                    "title": row.title,
                    "content": row.content[:400],
                    "url": row.url,
                    "published_at": row.published_at.isoformat() if row.published_at else None,
                    "collected_at": row.collected_at.isoformat() if row.collected_at else None,
                    "digested": row.digested,
                }
                for row in rows
            ]
        }


@router.get("/status")
async def scout_status():
    """Show configured sources, item counts, and collection cursors."""
    async with SessionLocal() as db:
        total = (await db.execute(select(func.count(ScoutItem.id)))).scalar_one()
        pending = (await db.execute(
            select(func.count(ScoutItem.id)).where(ScoutItem.digested == 0)
        )).scalar_one()
        states = (await db.execute(select(ScoutSourceState))).scalars().all()
        return {
            "configured_sources": configured_sources(),
            "total_items": total,
            "pending_items": pending,
            "source_cursors": [
                {
                    "source": s.source,
                    "last_seen_id": s.last_seen_id,
                    "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                }
                for s in states
            ],
        }
