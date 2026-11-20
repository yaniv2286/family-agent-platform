"""Tests for the Scout Ideas agent (agents/scout_ideas_agent.py).

All external collection (Telegram/Discord/RSS), the LLM call, and the
messenger send are mocked - these tests exercise the real endpoints, the
collection/dedup pipeline, cursor persistence, and the send-failure retry
path through the FastAPI client.
"""
import pytest_asyncio
from unittest.mock import AsyncMock

from sqlalchemy import delete, select

import tutors
from database import ScheduledReport, SessionLocal, ScoutItem, ScoutSourceState

APP_PIN = "test-pin-1234"
PIN_HEADERS = {"x-app-pin": APP_PIN}


import pytest


@pytest.fixture(autouse=True)
def _no_scout_env(monkeypatch):
    """Never let tests use the real SCOUT_* credentials from .env - the real
    .env is loaded by the app, and without this a test would open a live
    Telegram user session and collect real messages."""
    for var in (
        "SCOUT_TELEGRAM_API_ID",
        "SCOUT_TELEGRAM_API_HASH",
        "SCOUT_TELEGRAM_SESSION",
        "SCOUT_TELEGRAM_CHANNELS",
        "SCOUT_RSS_FEEDS",
        "SCOUT_DISCORD_BOT_TOKEN",
        "SCOUT_DISCORD_CHANNEL_IDS",
        "GEMINI_API_KEY",
        "SCOUT_GEMINI_MODEL",
        "SCOUT_RESEARCH_MAX_IDEAS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest_asyncio.fixture(autouse=True)
async def _clean_scout_tables():
    async with SessionLocal() as db:
        await db.execute(delete(ScoutItem))
        await db.execute(delete(ScoutSourceState))
        await db.execute(delete(ScheduledReport))
        await db.commit()
    yield


def _fake_collector(items, cursors=None):
    return AsyncMock(return_value=(list(items), dict(cursors or {})))


def _fake_item(source="telegram:@trader", external_id="10", content="NVDA is a buy"):
    return {
        "source": source,
        "external_id": external_id,
        "author": "@trader",
        "title": None,
        "content": content,
        "url": None,
        "published_at": None,
    }


def _patch_pipeline(monkeypatch, scout, items, cursors=None, send_ok=True, llm_text="💼 digest"):
    monkeypatch.setattr(
        scout,
        "_COLLECTORS",
        [_fake_collector(items, cursors), _fake_collector([]), _fake_collector([])],
    )
    send = AsyncMock(return_value=send_ok)
    monkeypatch.setattr(scout, "_send_via_messenger", send)
    llm = AsyncMock(return_value=llm_text)
    monkeypatch.setattr(tutors, "call_llm", llm)
    return send, llm


async def test_run_now_collects_summarizes_and_sends(client, monkeypatch):
    import agents.scout_ideas_agent as scout

    send, llm = _patch_pipeline(
        monkeypatch, scout, [_fake_item()], cursors={"telegram:@trader": "10"}
    )

    resp = await client.post("/api/scout/run-now", headers=PIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["items_collected"] == 1
    assert data["items_digested"] == 1
    assert data["telegram_sent"] is True
    assert data["skipped"] is False
    assert "💼 digest" in data["summary_text"]
    assert send.await_count == 1
    assert llm.await_count == 1

    async with SessionLocal() as db:
        state = await db.get(ScoutSourceState, "telegram:@trader")
        assert state is not None
        assert state.last_seen_id == "10"
        item = (await db.execute(select(ScoutItem))).scalar_one()
        assert item.digested == 1
        report = (await db.execute(select(ScheduledReport))).scalar_one()
        assert report.report_type == "investment_digest"
        assert report.sent_at is not None


async def test_run_now_dedupes_and_skips_when_nothing_new(client, monkeypatch):
    import agents.scout_ideas_agent as scout

    send, _ = _patch_pipeline(monkeypatch, scout, [_fake_item()])

    first = (await client.post("/api/scout/run-now", headers=PIN_HEADERS)).json()
    assert first["items_collected"] == 1
    assert first["skipped"] is False

    # Second run: the collector "sees" the same message again - dedup must
    # reject it and the already-digested item must not be resent.
    second = (await client.post("/api/scout/run-now", headers=PIN_HEADERS)).json()
    assert second["items_collected"] == 0
    assert second["skipped"] is True
    assert second["telegram_sent"] is False
    assert send.await_count == 1


async def test_failed_send_keeps_items_pending_for_retry(client, monkeypatch):
    import agents.scout_ideas_agent as scout

    send, _ = _patch_pipeline(monkeypatch, scout, [_fake_item()], send_ok=False)

    first = (await client.post("/api/scout/run-now", headers=PIN_HEADERS)).json()
    assert first["telegram_sent"] is False
    assert first["items_digested"] == 1

    async with SessionLocal() as db:
        item = (await db.execute(select(ScoutItem))).scalar_one()
        assert item.digested == 0

    # Next run must retry the same pending item even though collection
    # returns nothing new.
    monkeypatch.setattr(
        scout, "_COLLECTORS", [_fake_collector([]), _fake_collector([]), _fake_collector([])]
    )
    send.return_value = True
    second = (await client.post("/api/scout/run-now", headers=PIN_HEADERS)).json()
    assert second["items_collected"] == 0
    assert second["telegram_sent"] is True
    assert send.await_count == 2

    async with SessionLocal() as db:
        item = (await db.execute(select(ScoutItem))).scalar_one()
        assert item.digested == 1


async def test_run_now_without_any_source_skips_cleanly(client):
    resp = await client.post("/api/scout/run-now", headers=PIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["items_collected"] == 0
    assert data["skipped"] is True


async def test_status_endpoint_reports_config_and_counts(client, monkeypatch):
    import agents.scout_ideas_agent as scout

    _patch_pipeline(monkeypatch, scout, [_fake_item()])
    await client.post("/api/scout/run-now", headers=PIN_HEADERS)

    resp = await client.get("/api/scout/status", headers=PIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["configured_sources"].keys()) == {"telegram", "rss", "discord"}
    assert data["total_items"] == 1
    assert data["pending_items"] == 0
    assert isinstance(data["source_cursors"], list)


async def test_research_pipeline_uses_gemini_verdict_and_channel(client, monkeypatch):
    """With GEMINI_API_KEY set the digest switches to the research pipeline:
    candidates are extracted, each top candidate is deep-researched, and the
    message shows verdict + reasoning + the originating channel."""
    import agents.scout_ideas_agent as scout

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("SCOUT_RESEARCH_MAX_IDEAS", "8")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(scout, "_send_via_messenger", send)
    monkeypatch.setattr(
        scout,
        "_COLLECTORS",
        [_fake_collector([_fake_item()]), _fake_collector([]), _fake_collector([])],
    )
    monkeypatch.setattr(
        scout,
        "_extract_candidates",
        AsyncMock(return_value=[
            {
                "asset": "מלם תים",
                "direction": "long",
                "reason": "צמיחה ברווח ודיבידנד",
                "sources": ["@YKalphatrading"],
            }
        ]),
    )
    research = AsyncMock(return_value={
        "verdict": "מומלץ",
        "reasoning": "מכפיל רווח 12, צמיחת הכנסות 18%, יעד מחיר ממוצע גבוה ב-15% מהמחיר.",
        "confidence": "גבוהה",
        "citations": [],
    })
    monkeypatch.setattr(scout, "_research_candidate", research)

    resp = await client.post("/api/scout/run-now", headers=PIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["telegram_sent"] is True
    text = data["summary_text"]
    assert "מלם תים" in text
    assert "מומלץ" in text
    assert "מכפיל רווח 12" in text
    assert "@YKalphatrading" in text
    research.assert_awaited_once()


async def test_research_pipeline_falls_back_without_gemini_key(client, monkeypatch):
    """No GEMINI_API_KEY -> the plain digest path is used."""
    import agents.scout_ideas_agent as scout

    send, llm = _patch_pipeline(monkeypatch, scout, [_fake_item()])
    resp = await client.post("/api/scout/run-now", headers=PIN_HEADERS)
    data = resp.json()
    assert data["telegram_sent"] is True
    assert "💼 digest" in data["summary_text"]
    assert llm.await_count == 1


async def test_digest_skips_stale_and_samples_sources_fairly(client, monkeypatch):
    """Regression for the first-backfill bug: one hyper-active channel filled
    the whole 60-item digest because selection was strictly id-ordered.

    - Items older than SCOUT_STALE_HOURS are marked digested=2 (skipped), not sent.
    - Fresh items are picked round-robin per source so every channel is heard.
    """
    import agents.scout_ideas_agent as scout
    from datetime import datetime, timedelta

    async with SessionLocal() as db:
        # One stale item (5 days old) - must be skipped.
        db.add(ScoutItem(
            source="telegram:@a", external_id="old1", author="@a",
            content="OLD stock tip from last week",
            published_at=datetime.utcnow() - timedelta(days=5),
        ))
        # 70 fresh items from channel A + 5 from channel B.
        for i in range(70):
            db.add(ScoutItem(
                source="telegram:@a", external_id=f"a{i}", author="@a",
                content=f"idea from A {i}",
                published_at=datetime.utcnow(),
            ))
        for i in range(5):
            db.add(ScoutItem(
                source="telegram:@b", external_id=f"b{i}", author="@b",
                content=f"idea from B {i}",
                published_at=datetime.utcnow(),
            ))
        await db.commit()

    send, llm = _patch_pipeline(monkeypatch, scout, [])
    resp = await client.post("/api/scout/run-now", headers=PIN_HEADERS)
    data = resp.json()
    assert data["telegram_sent"] is True

    # The LLM user message lists sources as "מקור: <source>" - both channels
    # must appear, and the stale item must not.
    llm_block = llm.await_args.args[1][0]["content"]
    assert "telegram:@a" in llm_block
    assert "telegram:@b" in llm_block
    assert "OLD stock tip" not in llm_block

    async with SessionLocal() as db:
        stale = (await db.execute(
            select(ScoutItem).where(ScoutItem.external_id == "old1")
        )).scalar_one()
        assert stale.digested == 2


async def test_items_endpoint_lists_collected_items(client, monkeypatch):
    import agents.scout_ideas_agent as scout

    _patch_pipeline(
        monkeypatch,
        scout,
        [
            _fake_item(external_id="11"),
            _fake_item(source="rss:https://feed.example.com", external_id="guid-1"),
        ],
    )
    await client.post("/api/scout/run-now", headers=PIN_HEADERS)

    resp = await client.get("/api/scout/items", headers=PIN_HEADERS)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert {i["source"] for i in items} == {"telegram:@trader", "rss:https://feed.example.com"}

    pending = (await client.get("/api/scout/items?pending_only=true", headers=PIN_HEADERS)).json()["items"]
    assert pending == []  # all digested after successful send
