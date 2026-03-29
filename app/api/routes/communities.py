from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional, List

from app.core.deps import get_db, get_db_with_clerk_id
from app.core.auth import verify_token
from app.models.community import Community
from app.models.community_member import CommunityMember
from app.models.community_rule import CommunityRule
from app.models.user import User
from app.models.post import Post

router = APIRouter(prefix="/communities", tags=["communities"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class CreateCommunity(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    icon_url: Optional[str] = None
    banner_url: Optional[str] = None
    is_nsfw: bool = False
    is_private: bool = False


class CreateRule(BaseModel):
    title: str
    description: Optional[str] = None


# ── Formatters ─────────────────────────────────────────────────────────────────

def format_community(c: Community):
    return {
        "id": str(c.id),
        "name": c.name,
        "display_name": c.display_name,
        "description": c.description,
        "category": c.category if hasattr(c, "category") else None,
        "icon_url": c.icon_url,
        "banner_url": c.banner_url,
        "member_count": c.member_count,
        "is_nsfw": c.is_nsfw,
        "is_private": c.is_private,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def format_rule(r: CommunityRule):
    return {
        "id": str(r.id),
        "title": r.title,
        "description": r.description,
        "position": r.position,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def format_moderator(user: User):
    return {
        "id": str(user.id),
        "username": user.username,
        "avatar_url": user.avatar_url,
        "display_name": user.display_name,
    }


# ── Community CRUD ─────────────────────────────────────────────────────────────

@router.get("/")
def list_communities(db: Session = Depends(get_db)):
    communities = (
        db.query(Community)
        .filter(Community.is_private == False)
        .order_by(Community.member_count.desc())
        .limit(50)
        .all()
    )
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

    existing = db.query(Community).filter(Community.name == body.name.lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail="Community name already taken")

    community = Community(
        name=body.name.lower().strip(),
        display_name=body.display_name,
        description=body.description,
        icon_url=body.icon_url,
        banner_url=body.banner_url,
        is_nsfw=body.is_nsfw,
        is_private=body.is_private,
        created_by=user.id,
        member_count=1,
    )
    if hasattr(community, "category"):
        community.category = body.category

    db.add(community)
    db.flush()

    membership = CommunityMember(
        user_id=user.id,
        community_id=community.id,
        is_moderator=True,
    )
    db.add(membership)
    db.commit()
    db.refresh(community)

    return {"message": "Community created", "community": format_community(community)}


# ── Sidebar ────────────────────────────────────────────────────────────────────

@router.get("/{name}/sidebar")
def get_community_sidebar(name: str, db: Session = Depends(get_db)):
    c = db.query(Community).filter(Community.name == name).first()
    if not c:
        raise HTTPException(status_code=404, detail="Community not found")

    post_count = db.query(func.count(Post.id)).filter(Post.community_id == c.id).scalar() or 0

    rules = (
        db.query(CommunityRule)
        .filter(CommunityRule.community_id == c.id)
        .order_by(CommunityRule.position)
        .all()
    )

    mod_rows = (
        db.query(User)
        .join(CommunityMember, CommunityMember.user_id == User.id)
        .filter(
            CommunityMember.community_id == c.id,
            CommunityMember.is_moderator == True,
        )
        .all()
    )

    return {
        "community": format_community(c),
        "post_count": post_count,
        "rules": [format_rule(r) for r in rules],
        "moderators": [format_moderator(u) for u in mod_rows],
    }


# ── Rules CRUD ─────────────────────────────────────────────────────────────────

@router.get("/{name}/rules")
def list_rules(name: str, db: Session = Depends(get_db)):
    c = db.query(Community).filter(Community.name == name).first()
    if not c:
        raise HTTPException(status_code=404, detail="Community not found")
    rules = (
        db.query(CommunityRule)
        .filter(CommunityRule.community_id == c.id)
        .order_by(CommunityRule.position)
        .all()
    )
    return {"rules": [format_rule(r) for r in rules]}


@router.post("/{name}/rules")
def create_rule(
    name: str,
    body: CreateRule,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    c = db.query(Community).filter(Community.name == name).first()
    if not c:
        raise HTTPException(status_code=404, detail="Community not found")

    membership = db.query(CommunityMember).filter(
        CommunityMember.user_id == user.id,
        CommunityMember.community_id == c.id,
        CommunityMember.is_moderator == True,
    ).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Only moderators can add rules")

    max_pos = (
        db.query(func.max(CommunityRule.position))
        .filter(CommunityRule.community_id == c.id)
        .scalar()
    ) or -1

    rule = CommunityRule(
        community_id=c.id,
        title=body.title,
        description=body.description,
        position=max_pos + 1,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return {"rule": format_rule(rule)}


@router.delete("/{name}/rules/{rule_id}")
def delete_rule(
    name: str,
    rule_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    c = db.query(Community).filter(Community.name == name).first()
    if not c:
        raise HTTPException(status_code=404, detail="Community not found")

    membership = db.query(CommunityMember).filter(
        CommunityMember.user_id == user.id,
        CommunityMember.community_id == c.id,
        CommunityMember.is_moderator == True,
    ).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Only moderators can delete rules")

    rule = db.query(CommunityRule).filter(
        CommunityRule.id == rule_id,
        CommunityRule.community_id == c.id,
    ).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    db.delete(rule)
    db.commit()
    return {"message": "Rule deleted"}


# ── Moderators ─────────────────────────────────────────────────────────────────

@router.get("/{name}/moderators")
def list_moderators(name: str, db: Session = Depends(get_db)):
    c = db.query(Community).filter(Community.name == name).first()
    if not c:
        raise HTTPException(status_code=404, detail="Community not found")

    mods = (
        db.query(User)
        .join(CommunityMember, CommunityMember.user_id == User.id)
        .filter(
            CommunityMember.community_id == c.id,
            CommunityMember.is_moderator == True,
        )
        .all()
    )
    return {"moderators": [format_moderator(u) for u in mods]}


# ── Join / Leave ───────────────────────────────────────────────────────────────

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
        CommunityMember.community_id == community.id,
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
        CommunityMember.community_id == community.id,
    ).first()
    if not membership:
        raise HTTPException(status_code=400, detail="Not a member")

    db.delete(membership)
    community.member_count = max(0, community.member_count - 1)
    db.commit()
    return {"message": "Left community"}