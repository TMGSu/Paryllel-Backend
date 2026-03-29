from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, or_
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta

from app.core.deps import get_db, get_db_with_clerk_id
from app.core.auth import verify_token
from app.models.user import User
from app.models.community import Community
from app.models.community_member import CommunityMember
from app.models.post import Post
from app.models.post_report import PostReport
from app.models.community_ban import CommunityBan

try:
    from app.models.tip import Tip
    HAS_TIP = True
except ImportError:
    HAS_TIP = False

router = APIRouter(prefix="/admin", tags=["admin"])

# ── Admin guard ────────────────────────────────────────────────────────────────

ADMIN_CLERK_ID = "user_3BSBhTe2TZtLQAFZ51BQzJZEZ6B"

def require_admin(payload: dict):
    if payload.get("sub") != ADMIN_CLERK_ID:
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Audit log helper ───────────────────────────────────────────────────────────

def log_action(db: Session, actor_id, action: str, target_type: str = None, target_id: str = None, metadata: dict = {}):
    try:
        from sqlalchemy import text
        db.execute(
            text("INSERT INTO admin_audit_log (actor_id, action, target_type, target_id, metadata) VALUES (:actor_id, :action, :target_type, :target_id, :metadata::jsonb)"),
            {"actor_id": str(actor_id) if actor_id else None, "action": action, "target_type": target_type, "target_id": str(target_id) if target_id else None, "metadata": str(metadata).replace("'", '"')}
        )
        db.commit()
    except Exception:
        pass


# ── Formatters ─────────────────────────────────────────────────────────────────

def fmt_user(u: User) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "email": u.email,
        "display_name": u.display_name,
        "avatar_url": u.avatar_url,
        "reputation": u.reputation,
        "is_verified": u.is_verified,
        "is_banned": u.is_banned,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def fmt_community(c: Community, db: Session) -> dict:
    post_count = db.query(func.count(Post.id)).filter(Post.community_id == c.id, Post.is_removed == False).scalar() or 0
    mod_count = db.query(func.count(CommunityMember.id)).filter(CommunityMember.community_id == c.id, CommunityMember.is_moderator == True).scalar() or 0
    return {
        "id": str(c.id),
        "name": c.name,
        "display_name": c.display_name,
        "category": c.category if hasattr(c, "category") else None,
        "icon_url": c.icon_url,
        "member_count": c.member_count,
        "post_count": post_count,
        "mod_count": mod_count,
        "is_private": c.is_private,
        "is_nsfw": c.is_nsfw,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


# ── Stats ──────────────────────────────────────────────────────────────────────

@router.get("/stats")
def admin_stats(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    total_users = db.query(func.count(User.id)).scalar() or 0
    new_users_week = db.query(func.count(User.id)).filter(User.created_at >= week_ago).scalar() or 0
    banned_users = db.query(func.count(User.id)).filter(User.is_banned == True).scalar() or 0

    total_communities = db.query(func.count(Community.id)).scalar() or 0
    new_communities_week = db.query(func.count(Community.id)).filter(Community.created_at >= week_ago).scalar() or 0

    total_posts = db.query(func.count(Post.id)).filter(Post.is_removed == False).scalar() or 0
    new_posts_week = db.query(func.count(Post.id)).filter(Post.created_at >= week_ago, Post.is_removed == False).scalar() or 0
    removed_posts = db.query(func.count(Post.id)).filter(Post.is_removed == True).scalar() or 0

    pending_reports = db.query(func.count(PostReport.id)).filter(PostReport.status == "pending").scalar() or 0
    total_reports = db.query(func.count(PostReport.id)).scalar() or 0
    total_bans = db.query(func.count(CommunityBan.id)).scalar() or 0

    # Growth: users per day last 7 days
    growth = []
    for i in range(6, -1, -1):
        day_start = now - timedelta(days=i+1)
        day_end = now - timedelta(days=i)
        count = db.query(func.count(User.id)).filter(User.created_at >= day_start, User.created_at < day_end).scalar() or 0
        growth.append({"date": day_start.strftime("%b %d"), "users": count})

    return {
        "users": {"total": total_users, "new_this_week": new_users_week, "banned": banned_users},
        "communities": {"total": total_communities, "new_this_week": new_communities_week},
        "posts": {"total": total_posts, "new_this_week": new_posts_week, "removed": removed_posts},
        "moderation": {"pending_reports": pending_reports, "total_reports": total_reports, "total_bans": total_bans},
        "tips": {"total_volume": "$0.00", "this_month": "$0.00", "note": "Stripe integration pending"},
        "growth": growth,
    }


# ── Users ──────────────────────────────────────────────────────────────────────

@router.get("/users")
def admin_list_users(
    q: Optional[str] = Query(None),
    filter: str = Query("all"),  # all | banned | verified
    limit: int = Query(50, le=100),
    offset: int = Query(0),
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    query = db.query(User)

    if q:
        term = q.strip().lower()
        query = query.filter(or_(
            func.lower(User.username).contains(term),
            func.lower(User.email).contains(term),
            func.lower(User.display_name).contains(term),
        ))

    if filter == "banned":
        query = query.filter(User.is_banned == True)
    elif filter == "verified":
        query = query.filter(User.is_verified == True)

    total = query.count()
    users = query.order_by(desc(User.created_at)).offset(offset).limit(limit).all()

    return {"users": [fmt_user(u) for u in users], "total": total}


@router.patch("/users/{user_id}/ban")
def admin_ban_user(
    user_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_banned = not user.is_banned
    db.commit()

    action = "admin_ban_user" if user.is_banned else "admin_unban_user"
    log_action(db, None, action, "user", user_id, {"username": user.username})

    return {"message": f"User {'banned' if user.is_banned else 'unbanned'}", "is_banned": user.is_banned}


@router.patch("/users/{user_id}/verify")
def admin_verify_user(
    user_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_verified = not user.is_verified
    db.commit()
    log_action(db, None, "admin_verify_user", "user", user_id, {"username": user.username, "verified": user.is_verified})

    return {"message": f"User {'verified' if user.is_verified else 'unverified'}", "is_verified": user.is_verified}


@router.get("/users/{user_id}")
def admin_get_user(
    user_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    post_count = db.query(func.count(Post.id)).filter(Post.author_id == user.id, Post.is_removed == False).scalar() or 0
    community_count = db.query(func.count(CommunityMember.id)).filter(CommunityMember.user_id == user.id).scalar() or 0
    mod_count = db.query(func.count(CommunityMember.id)).filter(CommunityMember.user_id == user.id, CommunityMember.is_moderator == True).scalar() or 0

    recent_posts = db.query(Post).filter(Post.author_id == user.id, Post.is_removed == False).order_by(desc(Post.created_at)).limit(5).all()

    return {
        **fmt_user(user),
        "stats": {"post_count": post_count, "community_count": community_count, "mod_count": mod_count},
        "recent_posts": [{"id": str(p.id), "slug": p.slug, "title": p.title, "upvotes": p.upvotes, "created_at": p.created_at.isoformat()} for p in recent_posts],
    }


# ── Communities ────────────────────────────────────────────────────────────────

@router.get("/communities")
def admin_list_communities(
    q: Optional[str] = Query(None),
    filter: str = Query("all"),  # all | private | nsfw
    limit: int = Query(50, le=100),
    offset: int = Query(0),
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    query = db.query(Community)

    if q:
        term = q.strip().lower()
        query = query.filter(or_(
            func.lower(Community.name).contains(term),
            func.lower(Community.display_name).contains(term),
        ))

    if filter == "private":
        query = query.filter(Community.is_private == True)
    elif filter == "nsfw":
        query = query.filter(Community.is_nsfw == True)

    total = query.count()
    communities = query.order_by(desc(Community.member_count)).offset(offset).limit(limit).all()

    return {"communities": [fmt_community(c, db) for c in communities], "total": total}


@router.delete("/communities/{community_id}")
def admin_delete_community(
    community_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    community = db.query(Community).filter(Community.id == community_id).first()
    if not community:
        raise HTTPException(status_code=404, detail="Community not found")

    name = community.name
    db.delete(community)
    db.commit()
    log_action(db, None, "admin_delete_community", "community", community_id, {"name": name})

    return {"message": f"Community p/{name} deleted"}


@router.patch("/communities/{community_id}/nsfw")
def admin_toggle_nsfw(
    community_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    community = db.query(Community).filter(Community.id == community_id).first()
    if not community:
        raise HTTPException(status_code=404, detail="Community not found")

    community.is_nsfw = not community.is_nsfw
    db.commit()
    return {"message": f"Community NSFW set to {community.is_nsfw}", "is_nsfw": community.is_nsfw}


# ── Reports (cross-community) ──────────────────────────────────────────────────

@router.get("/reports")
def admin_list_reports(
    status: str = Query("pending"),
    limit: int = Query(50, le=100),
    offset: int = Query(0),
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    rows = (
        db.query(PostReport, Post, User, Community)
        .join(Post, Post.id == PostReport.post_id)
        .join(User, User.id == PostReport.reported_by)
        .join(Community, Community.id == PostReport.community_id)
        .filter(PostReport.status == status)
        .order_by(desc(PostReport.created_at))
        .offset(offset).limit(limit).all()
    )

    total = db.query(func.count(PostReport.id)).filter(PostReport.status == status).scalar() or 0

    return {
        "reports": [
            {
                "id": str(report.id),
                "reason": report.reason,
                "status": report.status,
                "created_at": report.created_at.isoformat() if report.created_at else None,
                "reported_by": {"username": reporter.username, "avatar_url": reporter.avatar_url},
                "community": {"name": community.name, "icon_url": community.icon_url},
                "post": {
                    "id": str(post.id), "slug": post.slug, "title": post.title,
                    "body": post.body, "upvotes": post.upvotes,
                    "author": post.author.username if post.author else None,
                },
            }
            for report, post, reporter, community in rows
        ],
        "total": total,
    }


@router.post("/reports/{report_id}/resolve")
def admin_resolve_report(
    report_id: str,
    action: str,  # resolved | dismissed | remove_post
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    report = db.query(PostReport).filter(PostReport.id == report_id).first()
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
        raise HTTPException(status_code=400, detail="Invalid action")

    db.commit()
    log_action(db, None, f"admin_report_{action}", "post", str(report.post_id))
    return {"message": f"Report {report.status}"}


# ── Posts ──────────────────────────────────────────────────────────────────────

@router.delete("/posts/{post_id}")
def admin_delete_post(
    post_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    post.is_removed = True
    db.commit()
    log_action(db, None, "admin_remove_post", "post", post_id, {"title": post.title})
    return {"message": "Post removed"}


# ── Audit log ──────────────────────────────────────────────────────────────────

@router.get("/audit-log")
def admin_audit_log(
    limit: int = Query(100, le=200),
    offset: int = Query(0),
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)
    get_db_with_clerk_id(payload["sub"], db)

    from sqlalchemy import text
    rows = db.execute(
        text("""
            SELECT a.id, a.action, a.target_type, a.target_id, a.metadata, a.created_at,
                   u.username as actor_username
            FROM admin_audit_log a
            LEFT JOIN users u ON u.id = a.actor_id
            ORDER BY a.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        {"limit": limit, "offset": offset}
    ).fetchall()

    total = db.execute(text("SELECT COUNT(*) FROM admin_audit_log")).scalar() or 0

    return {
        "logs": [
            {
                "id": str(r.id),
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "metadata": r.metadata,
                "actor": r.actor_username or "admin",
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": total,
    }