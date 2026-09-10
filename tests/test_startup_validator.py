import os

import pytest

from scripts.startup_validator import validate, REQUIRED_ENV_VARS


@pytest.mark.asyncio
async def test_startup_validator_passes_with_required_env(monkeypatch):
    # Ensure all required vars are present with non-placeholder values.
    for key in REQUIRED_ENV_VARS:
        monkeypatch.setenv(key, f"test-{key.lower()}")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("TUTOR_HISTORY_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    result = await validate()
    assert result.ok is True


@pytest.mark.asyncio
async def test_startup_validator_fails_when_app_pin_missing(monkeypatch):
    if "APP_PIN" in os.environ:
        monkeypatch.delenv("APP_PIN")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("TUTOR_HISTORY_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    result = await validate()
    assert result.ok is False
    assert any("APP_PIN" in e for e in result.errors)


@pytest.mark.asyncio
async def test_startup_validator_fails_on_placeholder_app_pin(monkeypatch):
    monkeypatch.setenv("APP_PIN", "your_app_pin_here")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("TUTOR_HISTORY_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    result = await validate()
    assert result.ok is False
    assert any("APP_PIN" in e for e in result.errors)


@pytest.mark.asyncio
async def test_startup_validator_warns_on_missing_optional_secrets(monkeypatch):
    monkeypatch.setenv("APP_PIN", "test-pin")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("TUTOR_HISTORY_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    for key in ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
        if key in os.environ:
            monkeypatch.delenv(key)

    result = await validate()
    assert result.ok is True
    assert any("OPENAI_API_KEY" in w for w in result.warnings)
