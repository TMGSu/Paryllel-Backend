from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.core.deps import get_db, get_db_with_clerk_id
from app.core.auth import verify_token
from app.models.community import Community
from app.models.community_member import CommunityMember
from app.models.user import User

router = APIRouter(prefix="/communities", tags=["communities"])


class CreateCommunity(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_nsfw: bool = False
    is_private: bool = False


def format_community(c: Community):
    return {
        "id": str(c.id),
        "name": c.name,
        "display_name": c.display_name,
        "description": c.description,
        "icon_url": c.icon_url,
        "banner_url": c.banner_url,
        "member_count": c.member_count,
        "is_nsfw": c.is_nsfw,
        "is_private": c.is_private,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/")
def list_communities(db: Session = Depends(get_db)):
    communities = db.query(Community).filter(Community.is_private == False).order_by(Community.member_count.desc()).limit(50).all()
    return {"communities": [format_community(c) for c in communities]}


@router.get("/{name}")
def get_community(name: str, db: Session = Depends(get_db)):
    c = db.query(Community).filter(Community.name == name).first()
    if not c:
        raise HTTPException(status_code=404, detail="Community not found")
    return format_community(c)


@router.post("/")
def create_community(
    body: CreateCommunity,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check name is taken
    existing = db.query(Community).filter(Community.name == body.name.lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail="Community name already taken")

    community = Community(
        name=body.name.lower().strip(),
        display_name=body.display_name,
        description=body.description,
        is_nsfw=body.is_nsfw,
        is_private=body.is_private,
        created_by=user.id,
        member_count=1,
    )
    db.add(community)
    db.flush()

    # Auto-join creator as moderator
    membership = CommunityMember(
        user_id=user.id,
        community_id=community.id,
        is_moderator=True,
    )
    db.add(membership)
    db.commit()
    db.refresh(community)

    return {"message": "Community created", "community": format_community(community)}


@router.post("/{name}/join")
def join_community(
    name: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    community = db.query(Community).filter(Community.name == name).first()

    if not community:
        raise HTTPException(status_code=404, detail="Community not found")

    existing = db.query(CommunityMember).filter(
        CommunityMember.user_id == user.id,
        CommunityMember.community_id == community.id
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Already a member")

    membership = CommunityMember(user_id=user.id, community_id=community.id)
    db.add(membership)
    community.member_count += 1
    db.commit()

    return {"message": "Joined community"}


@router.delete("/{name}/leave")
def leave_community(
    name: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    community = db.query(Community).filter(Community.name == name).first()

    if not community:
        raise HTTPException(status_code=404, detail="Community not found")

    membership = db.query(CommunityMember).filter(
        CommunityMember.user_id == user.id,
        CommunityMember.community_id == community.id
    ).first()

    if not membership:
        raise HTTPException(status_code=400, detail="Not a member")

    db.delete(membership)
    community.member_count = max(0, community.member_count - 1)
    db.commit()

    return {"message": "Left community"}