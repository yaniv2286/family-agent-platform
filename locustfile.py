"""Load test for the Koko /api/tutor/chat endpoint.

Run with:
    1. Make sure the backend is running and a child user exists.
    2. locust -f locustfile.py --host https://localhost:8000 -u 5 -r 5 --run-time 30s

If no child user exists yet, run:
    python tests/seed_for_locust.py
"""
import os
import uuid

from dotenv import load_dotenv
from locust import HttpUser, task, between

load_dotenv()

APP_PIN = os.getenv("APP_PIN", "1234")


class ChatUser(HttpUser):
    """A user that repeatedly posts chat messages to /api/tutor/chat."""

    wait_time = between(0, 0)
    user_id = None

    def on_start(self):
        # Verify the PIN and locate an existing child user.
        ping = self.client.get("/api/ping", headers={"x-app-pin": APP_PIN})
        if ping.status_code != 200:
            raise Exception(f"PIN auth failed: {ping.status_code}")

        users = self.client.get("/api/users", headers={"x-app-pin": APP_PIN})
        if users.status_code != 200:
            raise Exception(f"Failed to fetch users: {users.status_code}")
        payload = users.json()
        if not payload:
            raise Exception("No users found. Run: python tests/seed_for_locust.py")
        self.user_id = payload[0]["id"]

    @task
    def chat(self):
        payload = {
            "user_id": self.user_id,
            "subject": "math",
            "messages": [{"role": "user", "content": "כמה זה 7+2?"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        self.client.post(
            "/api/tutor/chat",
            json=payload,
            headers={"x-app-pin": APP_PIN},
            name="/api/tutor/chat",
        )
