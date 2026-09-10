import asyncio
from datetime import datetime

from sqlalchemy import select, delete

from database import Base, engine, SessionLocal, User


async def seed_database():
    """Create tables and seed the database with child and parent profiles."""
    # Initialize the database schema
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as db:
        try:
            # Delete all existing users to ensure clean state
            await db.execute(delete(User))
            await db.commit()

            # Create the 4 child profiles.
            # NOTE: interests are intentionally left as None (unknown) - we must never
            # assume a child's hobbies before they tell the tutor themselves. The tutor's
            # onboarding flow will ask each child directly and store what they actually say.
            children = [
                User(name="נויה", role="child", grade_level="5th Grade", interests=None, created_at=datetime.utcnow()),
                User(name="ינאי", role="child", grade_level="3rd Grade", interests=None, created_at=datetime.utcnow()),
                User(name="לביא", role="child", grade_level="1st Grade", interests=None, created_at=datetime.utcnow()),
                User(name="ליבי", role="child", grade_level="Kindergarten", interests=None, created_at=datetime.utcnow()),
            ]

            # Create parent profile
            parent = User(name="הורה", role="parent", grade_level=None, interests=None, created_at=datetime.utcnow())

            # Add all users to the session
            db.add_all(children)
            db.add(parent)

            # Commit the changes
            await db.commit()

            print("Database seeded successfully!")
            print(f"Added {len(children)} child profiles and 1 parent profile.")

        except Exception as e:
            await db.rollback()
            print(f"Error seeding database: {e}")
            raise


if __name__ == "__main__":
    asyncio.run(seed_database())
