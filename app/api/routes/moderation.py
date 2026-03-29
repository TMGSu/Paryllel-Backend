from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel
from typing import Optional

from app.core.deps import get_db, get_db_with_clerk_id
from app.core.auth import verify_token
from app.models.community import Community
from app.models.community_member import CommunityMember
from app.models.community_rule import CommunityRule
from app.models.community_ban import CommunityBan
from app.models.post_report import PostReport
from app.models.post import Post
from app.models.user import User

router = APIRouter(prefix="/mod", tags=["moderation"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_community_or_404(name: str, db: Session) -> Community:
    c = db.query(Community).filter(Community.name == name).first()
    if not c:
        raise HTTPException(status_code=404, detail="Community not found")
    return c


def require_mod(user: User, community: Community, db: Session):
    m = db.query(CommunityMember).filter(
        CommunityMember.user_id == user.id,
        CommunityMember.community_id == community.id,
        CommunityMember.is_moderator == True,
    ).first()
    if not m:
        raise HTTPException(status_code=403, detail="Moderators only")


def get_authed_user(clerk_user_id: str, db: Session) -> User:
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def fmt_user(u: User) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "display_name": u.display_name,
        "avatar_url": u.avatar_url,
        "reputation": u.reputation,
    }


def fmt_member(cm: CommunityMember, user: User) -> dict:
    return {
        "id": str(cm.id),
        "user": fmt_user(user),
        "is_moderator": cm.is_moderator,
        "joined_at": cm.joined_at.isoformat() if cm.joined_at else None,
    }


def fmt_post(p: Post) -> dict:
    return {
        "id": str(p.id),
        "slug": p.slug,
        "title": p.title,
        "body": p.body,
        "upvotes": p.upvotes,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "author": fmt_user(p.author) if p.author else None,
    }


# ── Schemas ────────────────────────────────────────────────────────────────────

class UpdateCommunity(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    icon_url: Optional[str] = None
    banner_url: Optional[str] = None
    is_nsfw: Optional[bool] = None
    is_private: Optional[bool] = None


class InviteMod(BaseModel):
    username: str


class BanUser(BaseModel):
    username: str
    reason: Optional[str] = None


class ReportPost(BaseModel):
    reason: str


class UpdateRule(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    position: Optional[int] = None


# ── Overview ───────────────────────────────────────────────────────────────────

@router.get("/{name}/overview")
def mod_overview(
    name: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    """Single endpoint for the mod dashboard — counts + basic info."""
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    member_count = community.member_count
    mod_count = db.query(func.count(CommunityMember.id)).filter(
        CommunityMember.community_id == community.id,
        CommunityMember.is_moderator == True,
    ).scalar() or 0
    pending_reports = db.query(func.count(PostReport.id)).filter(
        PostReport.community_id == community.id,
        PostReport.status == "pending",
    ).scalar() or 0
    ban_count = db.query(func.count(CommunityBan.id)).filter(
        CommunityBan.community_id == community.id,
    ).scalar() or 0
    rule_count = db.query(func.count(CommunityRule.id)).filter(
        CommunityRule.community_id == community.id,
    ).scalar() or 0

    return {
        "community": {
            "id": str(community.id),
            "name": community.name,
            "display_name": community.display_name,
            "description": community.description,
            "category": community.category if hasattr(community, "category") else None,
            "icon_url": community.icon_url,
            "banner_url": community.banner_url,
            "member_count": member_count,
            "is_nsfw": community.is_nsfw,
            "is_private": community.is_private,
            "created_at": community.created_at.isoformat() if community.created_at else None,
        },
        "stats": {
            "member_count": member_count,
            "mod_count": mod_count,
            "pending_reports": pending_reports,
            "ban_count": ban_count,
            "rule_count": rule_count,
        },
    }


# ── General Settings ───────────────────────────────────────────────────────────

@router.patch("/{name}/settings")
def update_community_settings(
    name: str,
    body: UpdateCommunity,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    if body.display_name is not None:
        community.display_name = body.display_name
    if body.description is not None:
        community.description = body.description
    if body.category is not None and hasattr(community, "category"):
        community.category = body.category
    if body.icon_url is not None:
        community.icon_url = body.icon_url
    if body.banner_url is not None:
        community.banner_url = body.banner_url
    if body.is_nsfw is not None:
        community.is_nsfw = body.is_nsfw
    if body.is_private is not None:
        community.is_private = body.is_private

    db.commit()
    db.refresh(community)
    return {"message": "Settings updated"}


# ── Mods & Members ─────────────────────────────────────────────────────────────

@router.get("/{name}/members")
def list_members(
    name: str,
    limit: int = 50,
    offset: int = 0,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    rows = (
        db.query(CommunityMember, User)
        .join(User, User.id == CommunityMember.user_id)
        .filter(CommunityMember.community_id == community.id)
        .order_by(CommunityMember.is_moderator.desc(), CommunityMember.joined_at)
        .offset(offset).limit(limit).all()
    )

    mods = [fmt_member(cm, u) for cm, u in rows if cm.is_moderator]
    members = [fmt_member(cm, u) for cm, u in rows if not cm.is_moderator]

    return {"mods": mods, "members": members}


@router.post("/{name}/mods/invite")
def invite_mod(
    name: str,
    body: InviteMod,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    target = db.query(User).filter(User.username == body.username).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    membership = db.query(CommunityMember).filter(
        CommunityMember.user_id == target.id,
        CommunityMember.community_id == community.id,
    ).first()

    if membership:
        if membership.is_moderator:
            raise HTTPException(status_code=400, detail="Already a moderator")
        membership.is_moderator = True
    else:
        membership = CommunityMember(
            user_id=target.id,
            community_id=community.id,
            is_moderator=True,
        )
        db.add(membership)
        community.member_count += 1

    db.commit()
    return {"message": f"u/{body.username} is now a moderator"}


@router.delete("/{name}/mods/{username}")
def remove_mod(
    name: str,
    username: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    target = db.query(User).filter(User.username == username).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Can't demote yourself if you're the only mod
    if str(target.id) == str(user.id):
        mod_count = db.query(func.count(CommunityMember.id)).filter(
            CommunityMember.community_id == community.id,
            CommunityMember.is_moderator == True,
        ).scalar()
        if mod_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove the only moderator")

    membership = db.query(CommunityMember).filter(
        CommunityMember.user_id == target.id,
        CommunityMember.community_id == community.id,
    ).first()

    if not membership or not membership.is_moderator:
        raise HTTPException(status_code=404, detail="Not a moderator")

    membership.is_moderator = False
    db.commit()
    return {"message": f"u/{username} removed as moderator"}


# ── Rules ──────────────────────────────────────────────────────────────────────

@router.get("/{name}/rules")
def mod_list_rules(
    name: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    rules = (
        db.query(CommunityRule)
        .filter(CommunityRule.community_id == community.id)
        .order_by(CommunityRule.position)
        .all()
    )
    return {"rules": [{"id": str(r.id), "title": r.title, "description": r.description, "position": r.position} for r in rules]}


@router.patch("/{name}/rules/{rule_id}")
def update_rule(
    name: str,
    rule_id: str,
    body: UpdateRule,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    rule = db.query(CommunityRule).filter(
        CommunityRule.id == rule_id,
        CommunityRule.community_id == community.id,
    ).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    if body.title is not None:
        rule.title = body.title
    if body.description is not None:
        rule.description = body.description
    if body.position is not None:
        rule.position = body.position

    db.commit()
    return {"message": "Rule updated"}


# ── Bans ───────────────────────────────────────────────────────────────────────

@router.get("/{name}/bans")
def list_bans(
    name: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    rows = (
        db.query(CommunityBan, User)
        .join(User, User.id == CommunityBan.user_id)
        .filter(CommunityBan.community_id == community.id)
        .order_by(desc(CommunityBan.created_at))
        .all()
    )

    return {
        "bans": [
            {
                "id": str(ban.id),
                "user": fmt_user(u),
                "reason": ban.reason,
                "created_at": ban.created_at.isoformat() if ban.created_at else None,
            }
            for ban, u in rows
        ]
    }


@router.post("/{name}/bans")
def ban_user(
    name: str,
    body: BanUser,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    target = db.query(User).filter(User.username == body.username).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if str(target.id) == str(user.id):
        raise HTTPException(status_code=400, detail="Cannot ban yourself")

    existing = db.query(CommunityBan).filter(
        CommunityBan.community_id == community.id,
        CommunityBan.user_id == target.id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="User is already banned")

    ban = CommunityBan(
        community_id=community.id,
        user_id=target.id,
        banned_by=user.id,
        reason=body.reason,
    )
    db.add(ban)

    # Remove from community if member
    membership = db.query(CommunityMember).filter(
        CommunityMember.community_id == community.id,
        CommunityMember.user_id == target.id,
    ).first()
    if membership:
        db.delete(membership)
        community.member_count = max(0, community.member_count - 1)

    db.commit()
    return {"message": f"u/{body.username} has been banned"}


@router.delete("/{name}/bans/{username}")
def unban_user(
    name: str,
    username: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    target = db.query(User).filter(User.username == username).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    ban = db.query(CommunityBan).filter(
        CommunityBan.community_id == community.id,
        CommunityBan.user_id == target.id,
    ).first()
    if not ban:
        raise HTTPException(status_code=404, detail="Ban not found")

    db.delete(ban)
    db.commit()
    return {"message": f"u/{username} has been unbanned"}


# ── Reports ────────────────────────────────────────────────────────────────────

@router.get("/{name}/reports")
def list_reports(
    name: str,
    status: str = "pending",
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    rows = (
        db.query(PostReport, Post, User)
        .join(Post, Post.id == PostReport.post_id)
        .join(User, User.id == PostReport.reported_by)
        .filter(
            PostReport.community_id == community.id,
            PostReport.status == status,
        )
        .order_by(desc(PostReport.created_at))
        .all()
    )

    return {
        "reports": [
            {
                "id": str(report.id),
                "reason": report.reason,
                "status": report.status,
                "created_at": report.created_at.isoformat() if report.created_at else None,
                "reported_by": fmt_user(reporter),
                "post": fmt_post(post),
            }
            for report, post, reporter in rows
        ]
    }


@router.post("/{name}/reports/{report_id}/resolve")
def resolve_report(
    name: str,
    report_id: str,
    action: str,  # "resolved" | "dismissed" | "remove_post"
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)
    community = get_community_or_404(name, db)
    require_mod(user, community, db)

    report = db.query(PostReport).filter(
        PostReport.id == report_id,
        PostReport.community_id == community.id,
    ).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    if action == "remove_post":
        post = db.query(Post).filter(Post.id == report.post_id).first()
        if post:
            post.is_removed = True
        report.status = "resolved"
    elif action in ("resolved", "dismissed"):
        report.status = action
    else:
        raise HTTPException(status_code=400, detail="Invalid action. Use: resolved, dismissed, remove_post")

    report.resolved_by = user.id
    db.commit()
    return {"message": f"Report {report.status}"}


# ── Submit report (any user) ───────────────────────────────────────────────────

@router.post("/report/{post_id}")
def report_post(
    post_id: str,
    body: ReportPost,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = get_authed_user(clerk_user_id, db)

    post = db.query(Post).filter(Post.id == post_id, Post.is_removed == False).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    existing = db.query(PostReport).filter(
        PostReport.post_id == post.id,
        PostReport.reported_by == user.id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Already reported")

    report = PostReport(
        post_id=post.id,
        community_id=post.community_id,
        reported_by=user.id,
        reason=body.reason,
    )
    db.add(report)
    db.commit()
    return {"message": "Post reported"}