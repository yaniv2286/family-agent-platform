import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from database import (
    ChatHistory,
    StudentProfile,
    TutorHistorySessionLocal,
    get_latest_parent_feedback,
    tutor_history_engine,
)

APP_PIN = "test-pin-1234"


class TestTTS:
    async def test_speech_requires_pin(self, client):
        response = await client.post("/api/tutor/speech", json={"text": "שלום"})
        assert response.status_code == 401

    async def test_speech_returns_mp3(self, client, mock_openai):
        response = await client.post(
            "/api/tutor/speech",
            json={"text": "שלום", "subject": "math"},
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.content == b"fake-mp3-bytes"
        # math voice is "shimmer" by default
        call = mock_openai.audio.speech.create.call_args
        assert call.kwargs["voice"] == "shimmer"
        assert call.kwargs["model"] == "tts-1"


class TestEndSession:
    async def test_end_session_creates_learning_log(self, client, child_user):
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [
                {"role": "user", "content": "2 plus 2"},
                {"role": "assistant", "content": "4"},
            ],
            "duration_minutes": 5,
        }
        response = await client.post(
            "/api/tutor/end-session",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == child_user.id
        assert data["subject"] == "math"
        assert data["topic"] == "addition"
        assert data["session_duration_minutes"] == 5


class TestHistory:
    async def test_history_requires_pin(self, client, child_user):
        response = await client.get(f"/api/history/{child_user.name}")
        assert response.status_code == 401

    async def test_history_returns_messages(self, client, child_user):
        now = datetime.now(timezone.utc)
        async with TutorHistorySessionLocal() as session:
            session.add(
                ChatHistory(
                    child_name=child_user.name,
                    subject="math",
                    role="user",
                    content="שלום",
                    timestamp=now,
                )
            )
            session.add(
                ChatHistory(
                    child_name=child_user.name,
                    subject="math",
                    role="assistant",
                    content="היי!",
                    timestamp=now,
                )
            )
            await session.commit()

        response = await client.get(
            f"/api/history/{child_user.name}?subject=math",
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][1]["role"] == "assistant"


class TestEnglish:
    async def test_english_idempotency_replays_previous_response(
        self, client, child_user, mock_openai
    ):
        key = str(uuid.uuid4())
        body = {
            "user_id": child_user.id,
            "messages": [{"role": "user", "content": "Hello"}],
            "idempotency_key": key,
        }

        first = await client.post(
            "/api/tutor/english",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["points_earned"] == 3

        second = await client.post(
            "/api/tutor/english",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        second_data = second.json()
        assert second_data["reply"] == first_data["reply"]
        assert second_data["points_earned"] == 0
        assert second_data["total_points"] == first_data["total_points"]


class TestChatValidation:
    async def test_chat_nonexistent_user(self, client, mock_openai):
        body = {
            "user_id": 999999,
            "subject": "math",
            "messages": [{"role": "user", "content": "שלום"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["points_earned"] == 0
        assert data["total_points"] == 0
        assert "לא מצאתי" in data["reply"] or "not found" in data["reply"].lower()


class TestPINMiddleware:
    @pytest.mark.parametrize(
        "method, url, body",
        [
            ("GET", "/api/ping", None),
            ("GET", "/api/users", None),
            ("POST", "/api/tutor/chat", {"user_id": 1, "subject": "math", "messages": [], "idempotency_key": str(uuid.uuid4())}),
            ("GET", "/api/history/NoOne", None),
            ("POST", "/api/tutor/speech", {"text": "שלום", "subject": "math"}),
            ("POST", "/api/tutor/end-session", {"user_id": 1, "subject": "math", "messages": [], "duration_minutes": 1}),
        ],
    )
    async def test_endpoint_requires_pin(self, client, method, url, body):
        if method == "GET":
            response = await client.get(url)
        else:
            response = await client.post(url, json=body or {})
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "method, url, body",
        [
            ("GET", "/api/ping", None),
            ("POST", "/api/tutor/chat", {"user_id": 1, "subject": "math", "messages": [], "idempotency_key": str(uuid.uuid4())}),
            ("GET", "/api/history/NoOne", None),
            ("POST", "/api/tutor/speech", {"text": "שלום", "subject": "math"}),
        ],
    )
    async def test_endpoint_accepts_valid_pin(self, client, method, url, body):
        if method == "GET":
            response = await client.get(url, headers={"x-app-pin": APP_PIN})
        else:
            response = await client.post(
                url,
                json=body or {},
                headers={"x-app-pin": APP_PIN},
            )
        assert response.status_code != 401


class TestLLMFallbacks:
    async def test_malformed_llm_response_returns_one_point(
        self, client, child_user, mock_openai
    ):
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="this is not json"))],
            usage=MagicMock(model_dump=lambda: {}),
        )
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "שלום"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["reply"] == "this is not json"
        assert data["points_earned"] == 1

    async def test_negative_points_gets_hard_capped_to_one(
        self, client, child_user, mock_openai
    ):
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content='{"reply_text": "ככה", "points_earned": -100}'
                    )
                )
            ],
            usage=MagicMock(model_dump=lambda: {}),
        )
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "שלום"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["points_earned"] == 1


class TestSubjectSeparation:
    async def test_math_and_english_history_are_separate(
        self, client, child_user, mock_openai
    ):
        math_body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "1+1"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        english_body = {
            "user_id": child_user.id,
            "messages": [{"role": "user", "content": "cat"}],
            "idempotency_key": str(uuid.uuid4()),
        }

        math_resp = await client.post(
            "/api/tutor/chat",
            json=math_body,
            headers={"x-app-pin": APP_PIN},
        )
        english_resp = await client.post(
            "/api/tutor/english",
            json=english_body,
            headers={"x-app-pin": APP_PIN},
        )
        assert math_resp.json()["total_points"] == 3
        assert english_resp.json()["total_points"] == 3

        math_hist = await client.get(
            f"/api/history/{child_user.name}?subject=math",
            headers={"x-app-pin": APP_PIN},
        )
        english_hist = await client.get(
            f"/api/history/{child_user.name}?subject=english",
            headers={"x-app-pin": APP_PIN},
        )
        assert len(math_hist.json()["messages"]) == 2
        assert len(english_hist.json()["messages"]) == 2


class TestOrchestrator:
    async def test_israel_day_is_converted_to_utc_bounds(self, monkeypatch):
        import orchestrator

        israel_time = datetime(2026, 9, 1, 21, 30, tzinfo=ZoneInfo("Asia/Jerusalem"))
        monkeypatch.setattr(orchestrator, "_local_now", lambda: israel_time)

        since, until = orchestrator._today_utc_bounds()

        assert since == datetime(2026, 8, 31, 21, 0)
        assert until.date() == datetime(2026, 9, 1).date()
        assert until.hour == 20
        assert until.minute == 59

    async def test_telegram_feedback_is_stored_per_child_and_acknowledged(
        self, monkeypatch
    ):
        from agents import messenger_agent

        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
        monkeypatch.setattr(
            messenger_agent,
            "_get_children",
            AsyncMock(
                return_value=[SimpleNamespace(name="נויה"), SimpleNamespace(name="ינאי")]
            ),
        )
        send_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(messenger_agent, "send_telegram_message", send_mock)

        await messenger_agent.check_telegram_feedback(
            {
                "update_id": 987654,
                "message": {
                    "chat": {"id": 123456},
                    "from": {"is_bot": False},
                    "text": "נויה - לחזק תרגילי לוח הכפל\nינאי - לתרגל אנגלית",
                },
            }
        )

        noya_directives = await get_latest_parent_feedback("נויה", "math")
        yanai_directives = await get_latest_parent_feedback("ינאי", "english")
        assert "נויה - לחזק תרגילי לוח הכפל" in noya_directives
        assert "ינאי - לתרגל אנגלית" in yanai_directives
        acknowledgement = send_mock.await_args.args[0]
        assert "ההנחיות התקבלו ונשמרו" in acknowledgement
        assert "נויה / חשבון" in acknowledgement
        assert "ינאי / אנגלית" in acknowledgement

    async def test_daily_report_lock_prevents_duplicate_telegrams(self, monkeypatch):
        import orchestrator

        # Pin "today" to a deterministic date so this test does not collide with
        # other tests that may have acquired the daily-report lock.
        fixed_now = datetime(2030, 1, 1, 21, 30, tzinfo=ZoneInfo("Asia/Jerusalem"))
        monkeypatch.setattr(orchestrator, "_local_now", lambda: fixed_now)

        # Stub the messenger so no real Telegram calls are made.
        send_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(orchestrator, "_send_via_messenger", send_mock)

        # First run should acquire the lock and "send" the report.
        result1 = await orchestrator.run_daily_orchestration()
        assert result1["skipped"] is False
        assert result1["telegram_sent"] is True
        assert send_mock.call_count == 1

        # Second run must be skipped by the daily-report lock and never send.
        result2 = await orchestrator.run_daily_orchestration()
        assert result2["skipped"] is True
        assert result2["telegram_sent"] is False
        assert send_mock.call_count == 1


class TestDatabaseMigrations:
    async def test_chat_history_has_required_columns(self, setup_database):
        async with tutor_history_engine.begin() as conn:
            result = await conn.execute(text("PRAGMA table_info(chat_history)"))
            columns = {row[1] for row in result.fetchall()}
        assert "id" in columns
        assert "child_name" in columns
        assert "subject" in columns
        assert "content" in columns
        assert "idempotency_key" in columns

    async def test_student_profiles_has_total_points(self, setup_database):
        # student_profiles lives in the tutor-history database (per-sub-child data).
        async with tutor_history_engine.begin() as conn:
            result = await conn.execute(text("PRAGMA table_info(student_profiles)"))
            columns = {row[1] for row in result.fetchall()}
        assert "total_points" in columns

    async def test_idempotency_unique_index_exists(self, setup_database):
        async with tutor_history_engine.begin() as conn:
            result = await conn.execute(text("PRAGMA index_list(chat_history)"))
            indexes = {row[1] for row in result.fetchall()}
        assert "uq_chat_idempotency" in indexes
