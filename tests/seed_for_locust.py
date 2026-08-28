"""One-time helper to create a child user for the Locust load test."""
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

import database as db
from database import User, SessionLocal


async def seed_locust_user():
    await db.init_db()
    await db.init_tutor_history_db()

    async with SessionLocal() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.name == "LocustChild"))
        existing = result.scalar_one_or_none()
        if existing:
            print(f"Locust user already exists: id={existing.id}")
            return existing.id

        user = User(
            name="LocustChild",
            role="child",
            grade_level="כיתה א",
            interests="football",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        print(f"Created locust user: id={user.id}")
        return user.id


if __name__ == "__main__":
    user_id = asyncio.run(seed_locust_user())
    sys.exit(0)
