from app.core.database import SessionLocal
from sqlalchemy.orm import Session
from fastapi import Depends


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_with_clerk_id(clerk_user_id: str, db: Session):
    """Set the JWT claim so RLS policies work"""
    db.execute(
        __import__('sqlalchemy').text(
            f"SELECT set_config('request.jwt.claim.sub', :clerk_id, true)"
        ),
        {"clerk_id": clerk_user_id}
    )
    return db