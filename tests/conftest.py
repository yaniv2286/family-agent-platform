import os
import shutil
import tempfile
import uuid

import pytest
import pytest_asyncio

# Set test environment *before* any local import so database.py and main.py
# build their engines/clients from these values rather than the real .env DBs.
os.environ.setdefault("APP_PIN", "test-pin-1234")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")

_test_db_dir = tempfile.mkdtemp(prefix="koko-tests-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_test_db_dir}/family_platform_test.db"
os.environ["TUTOR_HISTORY_DATABASE_URL"] = f"sqlite+aiosqlite:///{_test_db_dir}/tutor_history_test.db"

# database.py creates its engines at import time, so it must be imported after
# the environment variables above are set.
import database as db  # noqa: E402
from database import User, SessionLocal  # noqa: E402


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_database():
    """Create the test DB schema once per test session and clean up afterwards."""
    await db.init_db()
    await db.init_tutor_history_db()
    yield
    await db.engine.dispose()
    await db.tutor_history_engine.dispose()
    shutil.rmtree(_test_db_dir, ignore_errors=True)


@pytest.fixture
def app(monkeypatch):
    import main
    import scheduler as sched
    import tutors as t

    # Prevent the real APScheduler from running during tests.
    monkeypatch.setattr(main, "start_scheduler", lambda: None)
    monkeypatch.setattr(main, "stop_scheduler", lambda: None)
    monkeypatch.setattr(sched, "start_scheduler", lambda: None)
    monkeypatch.setattr(sched, "stop_scheduler", lambda: None)

    # Ensure no stale OpenAI client or model-unavailability set is carried over.
    t._async_client = None
    t._unavailable_models.clear()

    return main.app


@pytest_asyncio.fixture
async def client(app):
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def child_user(client):
    async with SessionLocal() as session:
        unique_name = f"TestChild-{uuid.uuid4().hex[:8]}"
        user = User(
            name=unique_name,
            role="child",
            grade_level="כיתה א",
            interests="football",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
def mock_openai(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    import tutors as t

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content='{"reply_text": "תשובת ניסיון", "points_earned": 3}'
                    )
                )
            ],
            usage=MagicMock(model_dump=lambda: {}),
        )
    )
    fake_client.audio.transcriptions.create = AsyncMock(
        return_value=MagicMock(text="שלום")
    )
    fake_client.audio.speech.create = AsyncMock(
        return_value=MagicMock(content=b"fake-mp3-bytes")
    )
    monkeypatch.setattr(t, "_async_client", fake_client)
    return fake_client
