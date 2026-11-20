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
