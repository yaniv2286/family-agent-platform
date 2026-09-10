"""Koko startup validator.

Exits with a non-zero code and a plain-text report if any critical
configuration, secret, or dependency is missing. Warnings are printed but do
not block startup.

Run manually:
    python scripts/startup_validator.py

Or import and call validate() from the entrypoint before starting uvicorn.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text


@dataclass
class ValidationResult:
    ok: bool
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)


REQUIRED_ENV_VARS = {
    "APP_PIN": "PIN used by parents/children to authenticate with the API",
}

OPTIONAL_ENV_VARS = {
    "OPENAI_API_KEY": "Required for live LLM chat; tutors return a fallback message when missing",
    "TELEGRAM_BOT_TOKEN": "Required for Telegram alerts from the orchestrator",
    "TELEGRAM_CHAT_ID": "Required for Telegram alerts from the orchestrator",
    "AGENT_NEURALTRADER_URL": "Control-plane URL for NeuralTrader telemetry/commands",
    "AGENT_NEURALTRADER_SECRET": "HMAC secret for NeuralTrader control-plane calls",
    "AGENT_VOLATILITYHUNTER_URL": "Control-plane URL for VolatilityHunter telemetry/commands",
    "AGENT_VOLATILITYHUNTER_SECRET": "HMAC secret for VolatilityHunter control-plane calls",
}


def _get_database_url() -> str:
    default = "sqlite+aiosqlite:///./family_platform.db"
    url = os.getenv("DATABASE_URL", default)
    if url.startswith("sqlite:///") and not url.startswith("sqlite+aiosqlite:///"):
        url = url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


def _validate_env(result: ValidationResult) -> None:
    for key, description in REQUIRED_ENV_VARS.items():
        value = os.getenv(key)
        if not value or value.strip() == "" or "your" in value.lower():
            result.add_error(f"{key} is missing or looks like a placeholder ({description})")

    for key, description in OPTIONAL_ENV_VARS.items():
        value = os.getenv(key)
        if not value or value.strip() == "" or "your" in value.lower():
            result.add_warning(f"{key} is not configured ({description})")


async def _validate_main_database(result: ValidationResult) -> None:
    url = _get_database_url()
    try:
        engine = create_async_engine(url, future=True)
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
    except Exception as exc:
        result.add_error(f"Main database connection failed ({url}): {exc}")


async def _validate_tutor_history_database(result: ValidationResult) -> None:
    default = "sqlite+aiosqlite:///./tutor_history.db"
    url = os.getenv("TUTOR_HISTORY_DATABASE_URL", default)
    if url.startswith("sqlite:///") and not url.startswith("sqlite+aiosqlite:///"):
        url = url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    try:
        engine = create_async_engine(url, future=True)
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
    except Exception as exc:
        result.add_error(f"Tutor history database connection failed ({url}): {exc}")


async def validate() -> ValidationResult:
    """Run all startup checks and return a ValidationResult."""
    result = ValidationResult(ok=True)
    _validate_env(result)
    await _validate_main_database(result)
    await _validate_tutor_history_database(result)
    return result


def _print_report(result: ValidationResult) -> None:
    print("=" * 60)
    print("Koko Startup Validator")
    print("=" * 60)
    if result.warnings:
        print("\nWARNINGS (startup will continue):")
        for w in result.warnings:
            print(f"  - {w}")
    if result.errors:
        print("\nERRORS (startup blocked):")
        for e in result.errors:
            print(f"  - {e}")
    print("\n" + ("OK" if result.ok else "FAILED"))
    print("=" * 60)


async def main() -> int:
    result = await validate()
    _print_report(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
