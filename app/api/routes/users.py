from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.core.deps import get_db, get_db_with_clerk_id
from app.core.auth import verify_token
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])


class UpdateUser(BaseModel):
    username: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None


def format_user(user: User):
    return {
        "id": str(user.id),
        "clerk_user_id": user.clerk_user_id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "bio": user.bio,
        "avatar_url": user.avatar_url,
        "reputation": user.reputation,
        "total_earned": float(user.total_earned) if user.total_earned is not None else 0.0,
        "is_verified": user.is_verified,
        "is_banned": user.is_banned,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


@router.post("/me")
def create_or_get_user(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    email = payload.get("email")  # ✅ pull email from JWT

    print("📨 Payload:", payload)  # ← add this
    print("📧 Email:", email)       # ← add this

    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()

    if user:
        return {"message": "User exists", "user": format_user(user)}

    # ✅ store clerk_user_id + email on creation
    new_user = User(clerk_user_id=clerk_user_id, email=email)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return {"message": "User created", "user": format_user(new_user)}


@router.patch("/me")
def update_user(
    updates: UpdateUser,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]

    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if updates.username is not None:
        user.username = updates.username
    if updates.display_name is not None:
        user.display_name = updates.display_name
    if updates.avatar_url is not None:
        user.avatar_url = updates.avatar_url

    db.commit()
    db.refresh(user)

    return {"message": "User updated", "user": format_user(user)}