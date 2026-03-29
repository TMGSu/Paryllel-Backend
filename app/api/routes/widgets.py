from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel
from typing import Optional, Any
import json

from app.core.deps import get_db, get_db_with_clerk_id
from app.core.auth import verify_token
from app.models.community import Community
from app.models.community_member import CommunityMember
from app.models.community_widget import CommunityWidget
from app.models.community_rule import CommunityRule
from app.models.post import Post
from app.models.user import User
from app.models.vote import Vote

try:
    from app.models.tip import Tip
    HAS_TIP = True
except ImportError:
    HAS_TIP = False

router = APIRouter(prefix="/communities", tags=["widgets"])

VALID_WIDGET_TYPES = {
    "text",
    "links",
    "related_communities",
    "leaderboard",
    "rules_summary",
    "image_banner",
    "top_tippers",
    "community_stats",
    "events",
    "announcements",
    "weekly_theme",
}


# ── Schemas ────────────────────────────────────────────────────────────────────

class CreateWidget(BaseModel):
    widget_type: str
    title: Optional[str] = None
    position: Optional[int] = None
    config: Optional[dict] = {}


class UpdateWidget(BaseModel):
    title: Optional[str] = None
    position: Optional[int] = None
    config: Optional[dict] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def format_widget(w: CommunityWidget) -> dict:
    return {
        "id": str(w.id),
        "widget_type": w.widget_type,
        "title": w.title,
        "position": w.position,
        "config": w.config or {},
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


def get_community_or_404(name: str, db: Session) -> Community:
    c = db.query(Community).filter(Community.name == name).first()
    if not c:
        raise HTTPException(status_code=404, detail="Community not found")
    return c


def require_mod(user: User, community: Community, db: Session):
    membership = db.query(CommunityMember).filter(
        CommunityMember.user_id == user.id,
        CommunityMember.community_id == community.id,
        CommunityMember.is_moderator == True,
    ).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Only moderators can manage widgets")


def get_next_position(community_id, db: Session) -> int:
    max_pos = (
        db.query(func.max(CommunityWidget.position))
        .filter(CommunityWidget.community_id == community_id)
        .scalar()
    )
    return (max_pos or -1) + 1


# ── Widget data resolvers ──────────────────────────────────────────────────────

def resolve_leaderboard(community: Community, db: Session, limit: int = 5) -> list:
    """Top contributors by post upvotes in this community."""
    rows = (
        db.query(
            User.id,
            User.username,
            User.avatar_url,
            User.display_name,
            func.sum(Post.upvotes).label("total_upvotes"),
            func.count(Post.id).label("post_count"),
        )
        .join(Post, Post.author_id == User.id)
        .filter(Post.community_id == community.id, Post.is_removed == False)
        .group_by(User.id, User.username, User.avatar_url, User.display_name)
        .order_by(desc("total_upvotes"))
        .limit(limit)
        .all()
    )
    return [
        {
            "username": r.username,
            "display_name": r.display_name,
            "avatar_url": r.avatar_url,
            "total_upvotes": r.total_upvotes or 0,
            "post_count": r.post_count or 0,
        }
        for r in rows
    ]


def resolve_top_tippers(community: Community, db: Session, limit: int = 5) -> list:
    """Top tippers by total tips sent in this community."""
    if not HAS_TIP:
        return []
    try:
        rows = (
            db.query(
                User.username,
                User.avatar_url,
                User.display_name,
                func.sum(Tip.amount).label("total_tipped"),
            )
            .join(Tip, Tip.tipper_id == User.id)
            .join(Post, Post.id == Tip.post_id)
            .filter(Post.community_id == community.id)
            .group_by(User.username, User.avatar_url, User.display_name)
            .order_by(desc("total_tipped"))
            .limit(limit)
            .all()
        )
        return [
            {
                "username": r.username,
                "display_name": r.display_name,
                "avatar_url": r.avatar_url,
                "total_tipped": float(r.total_tipped or 0) / 100,
            }
            for r in rows
        ]
    except Exception:
        return []


def resolve_community_stats(community: Community, db: Session) -> dict:
    """Post count, member count, posts this week."""
    from datetime import datetime, timedelta
    one_week_ago = datetime.utcnow() - timedelta(days=7)

    total_posts = db.query(func.count(Post.id)).filter(
        Post.community_id == community.id,
        Post.is_removed == False,
    ).scalar() or 0

    posts_this_week = db.query(func.count(Post.id)).filter(
        Post.community_id == community.id,
        Post.is_removed == False,
        Post.created_at >= one_week_ago,
    ).scalar() or 0

    return {
        "member_count": community.member_count,
        "total_posts": total_posts,
        "posts_this_week": posts_this_week,
    }


def resolve_rules_summary(community: Community, db: Session) -> list:
    rules = (
        db.query(CommunityRule)
        .filter(CommunityRule.community_id == community.id)
        .order_by(CommunityRule.position)
        .limit(10)
        .all()
    )
    return [{"title": r.title, "description": r.description} for r in rules]


def enrich_widget(w: CommunityWidget, community: Community, db: Session) -> dict:
    """Add live data to dynamic widget types."""
    base = format_widget(w)

    if w.widget_type == "leaderboard":
        limit = w.config.get("limit", 5) if w.config else 5
        base["data"] = resolve_leaderboard(community, db, limit=limit)

    elif w.widget_type == "top_tippers":
        limit = w.config.get("limit", 5) if w.config else 5
        base["data"] = resolve_top_tippers(community, db, limit=limit)

    elif w.widget_type == "community_stats":
        base["data"] = resolve_community_stats(community, db)

    elif w.widget_type == "rules_summary":
        base["data"] = resolve_rules_summary(community, db)

    return base


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/{name}/widgets")
def list_widgets(name: str, db: Session = Depends(get_db)):
    """Get all widgets for a community, with live data resolved."""
    community = get_community_or_404(name, db)
    widgets = (
        db.query(CommunityWidget)
        .filter(CommunityWidget.community_id == community.id)
        .order_by(CommunityWidget.position)
        .all()
    )
    return {"widgets": [enrich_widget(w, community, db) for w in widgets]}


@router.post("/{name}/widgets")
def create_widget(
    name: str,
    body: CreateWidget,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    if body.widget_type not in VALID_WIDGET_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid widget type. Must be one of: {', '.join(sorted(VALID_WIDGET_TYPES))}"
        )

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    position = body.position if body.position is not None else get_next_position(community.id, db)

    widget = CommunityWidget(
        community_id=community.id,
        widget_type=body.widget_type,
        title=body.title or _default_title(body.widget_type),
        position=position,
        config=body.config or {},
    )
    db.add(widget)
    db.commit()
    db.refresh(widget)
    return {"widget": enrich_widget(widget, community, db)}


@router.patch("/{name}/widgets/{widget_id}")
def update_widget(
    name: str,
    widget_id: str,
    body: UpdateWidget,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    widget = db.query(CommunityWidget).filter(
        CommunityWidget.id == widget_id,
        CommunityWidget.community_id == community.id,
    ).first()
    if not widget:
        raise HTTPException(status_code=404, detail="Widget not found")

    if body.title is not None:
        widget.title = body.title
    if body.position is not None:
        widget.position = body.position
    if body.config is not None:
        widget.config = body.config

    db.commit()
    db.refresh(widget)
    return {"widget": enrich_widget(widget, community, db)}


@router.delete("/{name}/widgets/{widget_id}")
def delete_widget(
    name: str,
    widget_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    widget = db.query(CommunityWidget).filter(
        CommunityWidget.id == widget_id,
        CommunityWidget.community_id == community.id,
    ).first()
    if not widget:
        raise HTTPException(status_code=404, detail="Widget not found")

    db.delete(widget)
    db.commit()
    return {"message": "Widget deleted"}


@router.post("/{name}/widgets/reorder")
def reorder_widgets(
    name: str,
    ordered_ids: list[str],
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    """Pass an ordered list of widget IDs to set their positions."""
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    for i, widget_id in enumerate(ordered_ids):
        db.query(CommunityWidget).filter(
            CommunityWidget.id == widget_id,
            CommunityWidget.community_id == community.id,
        ).update({"position": i})

    db.commit()
    return {"message": "Widgets reordered"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _default_title(widget_type: str) -> str:
    return {
        "text": "About",
        "links": "Useful Links",
        "related_communities": "Related Communities",
        "leaderboard": "Top Contributors",
        "rules_summary": "Community Rules",
        "image_banner": "Banner",
        "top_tippers": "Top Tippers",
        "community_stats": "Community Stats",
        "events": "Upcoming Events",
        "announcements": "Announcements",
        "weekly_theme": "This Week",
    }.get(widget_type, widget_type.replace("_", " ").title())