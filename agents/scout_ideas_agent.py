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
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx
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

async def _collect_telegram() -> Tuple[List[dict], Dict[str, str]]:
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
        for chan in channels:
            source = f"telegram:{chan}"
            last_seen = await _get_last_seen_id(source)
            min_id = int(last_seen) if last_seen else 0
            max_id = min_id
            try:
                async for msg in client.iter_messages(chan, min_id=min_id, limit=MAX_MESSAGES_PER_CHANNEL):
                    max_id = max(max_id, msg.id)
                    text = (msg.message or "").strip()
                    if not text:
                        continue
                    items.append({
                        "source": source,
                        "external_id": str(msg.id),
                        "author": chan,
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

async def collect_all() -> int:
    """Run every collector, dedupe against scout_items, store new rows.

    Returns the number of new items stored.
    """
    collected: List[dict] = []
    cursors: Dict[str, str] = {}
    for collector in _COLLECTORS:
        try:
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


async def run_scout_digest() -> dict:
    """Entry point: collect new items, summarize pending items, send digest.

    Items stay digested=0 when the Telegram send fails so the next run picks
    them up again instead of losing them silently.
    """
    async with _DIGEST_LOCK:
        logger.info("Starting scout digest run")
        collected = await collect_all()

        pending_ids: List[int] = []
        pending_items: List[dict] = []
        async with SessionLocal() as db:
            rows = (await db.execute(
                select(ScoutItem)
                .where(ScoutItem.digested == 0)
                .order_by(ScoutItem.id)
                .limit(MAX_ITEMS_PER_DIGEST)
            )).scalars().all()
            # Capture plain dicts inside the session - reading ORM attributes
            # after commit triggers MissingGreenlet (see AGENTS.md).
            for row in rows:
                pending_ids.append(row.id)
                pending_items.append({
                    "source": row.source,
                    "author": row.author,
                    "title": row.title,
                    "content": row.content,
                    "url": row.url,
                })

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

        summary = await _summarize_items(pending_items)
        stamp = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
        digest_text = f"📈 Scout Ideas - {stamp} UTC\n{summary}"
        sent = await _send_via_messenger(digest_text)

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
    return await run_scout_digest()


agent_mesh.on_ask("scout_ideas", "collect_digest", handle_collect_digest)


@router.post("/run-now")
async def scout_run_now():
    """Manually trigger a collect + digest run."""
    try:
        return await run_scout_digest()
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
