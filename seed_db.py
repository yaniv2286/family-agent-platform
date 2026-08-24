from database import Base, engine, SessionLocal, User
from datetime import datetime


def seed_database():
    # Initialize the database
    Base.metadata.create_all(bind=engine)
    
    # Create a session
    db = SessionLocal()
    
    try:
        # Delete all existing users to ensure clean state
        db.query(User).delete()
        db.commit()
        
        # Create the 4 child profiles.
        # NOTE: interests are intentionally left as None (unknown) - we must never
        # assume a child's hobbies before they tell the tutor themselves. The tutor's
        # onboarding flow will ask each child directly and store what they actually say.
        children = [
            User(
                name="נויה",
                role="child",
                grade_level="כיתה א'",
                interests=None,
                created_at=datetime.utcnow()
            ),
            User(
                name="ינאי",
                role="child",
                grade_level="כיתה ג'",
                interests=None,
                created_at=datetime.utcnow()
            ),
            User(
                name="לביא",
                role="child",
                grade_level="כיתה ה'",
                interests=None,
                created_at=datetime.utcnow()
            ),
            User(
                name="ליבי",
                role="child",
                grade_level="כיתה ו'",
                interests=None,
                created_at=datetime.utcnow()
            )
        ]
        
        # Create parent profile
        parent = User(
            name="הורה",
            role="parent",
            grade_level=None,
            interests=None,
            created_at=datetime.utcnow()
        )
        
        # Add all users to the session
        db.add_all(children)
        db.add(parent)
        
        # Commit the changes
        db.commit()
        
        print("Database seeded successfully!")
        print(f"Added {len(children)} child profiles and 1 parent profile.")
        
    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
