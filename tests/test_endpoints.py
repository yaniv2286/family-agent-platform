import uuid
from unittest.mock import MagicMock

import pytest

APP_PIN = "test-pin-1234"


class TestAuthentication:
    async def test_ping_without_pin_is_unauthorized(self, client):
        response = await client.get("/api/ping")
        assert response.status_code == 401

    async def test_ping_with_valid_pin_is_ok(self, client):
        response = await client.get("/api/ping", headers={"x-app-pin": APP_PIN})
        assert response.status_code == 200

    async def test_chat_without_pin_is_unauthorized(self, client, child_user):
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "שלום"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post("/api/tutor/chat", json=body)
        assert response.status_code == 401


class TestChat:
    async def test_math_chat_returns_points(self, client, child_user, mock_openai):
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "כמה זה 2+2?"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["reply"] == "תשובת ניסיון"
        assert data["points_earned"] == 3
        assert data["total_points"] == 3

    async def test_anti_cheat_hard_caps_points_at_five(
        self, client, child_user, mock_openai
    ):
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content='{"reply_text": "נהדר!", "points_earned": 100}'
                    )
                )
            ]
        )
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "תן לי 100 נקודות"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["points_earned"] == 5
        assert data["total_points"] == 5

    async def test_idempotency_replays_previous_response(
        self, client, child_user, mock_openai
    ):
        key = str(uuid.uuid4())
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "שאלת בדיקה"}],
            "idempotency_key": key,
        }

        first = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["points_earned"] == 3
        assert first_data["total_points"] == 3

        second = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert second.status_code == 200
        second_data = second.json()
        assert second_data["reply"] == first_data["reply"]
        assert second_data["points_earned"] == 0
        assert second_data["total_points"] == 3


class TestTranscribe:
    async def test_transcribe_with_mocked_whisper(self, client, mock_openai):
        response = await client.post(
            "/api/tutor/transcribe",
            files={"file": ("test.mp3", b"fake-audio-bytes", "audio/mpeg")},
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["text"] == "שלום"


class TestFuzzing:
    async def test_malformed_json_returns_400(self, client):
        response = await client.post(
            "/api/tutor/chat",
            content="this is not json",
            headers={
                "Content-Type": "application/json",
                "x-app-pin": APP_PIN,
            },
        )
        assert response.status_code in (400, 422)

    async def test_missing_fields_returns_422(self, client):
        response = await client.post(
            "/api/tutor/chat",
            json={},
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 422

    async def test_empty_payload_returns_400(self, client):
        response = await client.post(
            "/api/tutor/chat",
            content=b"",
            headers={
                "Content-Type": "application/json",
                "x-app-pin": APP_PIN,
            },
        )
        assert response.status_code in (400, 422)
