from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, desc
from typing import Optional

from app.core.deps import get_db
from app.models.community import Community
from app.models.post import Post
from app.models.user import User
from app.models.community_subscription import CommunitySubscription
from app.core.auth import verify_token
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from datetime import datetime, timezone

optional_security = HTTPBearer(auto_error=False)

router = APIRouter(prefix="/search", tags=["search"])


# ── Formatters ─────────────────────────────────────────────────────────────────

def format_community(c: Community):
    return {
        "id": str(c.id),
        "name": c.name,
        "display_name": c.display_name,
        "description": c.description,
        "icon_url": c.icon_url,
        "banner_url": c.banner_url,
        "member_count": c.member_count,
        "category": c.category if hasattr(c, "category") else None,
        "is_private": c.is_private,
        "is_nsfw": c.is_nsfw,
    }


def format_post(p: Post):
    return {
        "id": str(p.id),
        "slug": p.slug,
        "title": p.title,
        "body": p.body,
        "upvotes": p.upvotes,
        "downvotes": p.downvotes,
        "comment_count": p.comment_count,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "author": {
            "username": p.author.username,
            "avatar_url": p.author.avatar_url,
        } if p.author else None,
        "community": {
            "name": p.community.name,
            "display_name": p.community.display_name,
            "icon_url": p.community.icon_url,
        } if p.community else None,
    }


def format_user(u: User):
    return {
        "id": str(u.id),
        "username": u.username,
        "display_name": u.display_name,
        "avatar_url": u.avatar_url,
        "bio": u.bio,
        "reputation": u.reputation,
        "is_verified": u.is_verified,
    }


# ── Community search ───────────────────────────────────────────────────────────

@router.get("/communities")
def search_communities(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, le=50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
):
    """
    Search communities by name, display_name, or description.
    Used by: create post dropdown, explore communities tab, global search.
    Only returns public communities.
    Results ranked: exact name match first, then starts-with, then contains.
    """
    term = q.strip().lower()

    results = (
        db.query(Community)
        .filter(
            Community.is_private == False,
            or_(
                func.lower(Community.name).contains(term),
                func.lower(Community.display_name).contains(term),
                func.lower(Community.description).contains(term),
            )
        )
        .order_by(
            # Exact name match first
            (func.lower(Community.name) == term).desc(),
            # Starts with term
            func.lower(Community.name).startswith(term).desc(),
            # Then by member count
            desc(Community.member_count),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "query": q,
        "communities": [format_community(c) for c in results],
        "count": len(results),
    }


# ── Post search ────────────────────────────────────────────────────────────────

@router.get("/posts")
def search_posts(
    q: str = Query(..., min_length=1),
    community: Optional[str] = Query(None),
    sort: str = Query("relevance"),
    limit: int = Query(20, le=50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
):
    """
    Search posts by title and body.
    Used by: explore posts tab, popular page, global search.
    Optionally filter to a specific community.
    """
    term = q.strip().lower()

    current_user = None
    if credentials:
        try:
            from jwt import PyJWKClient
            import jwt, os
            jwk_client = PyJWKClient(f"https://{os.getenv('CLERK_FRONTEND_API')}/.well-known/jwks.json")
            signing_key = jwk_client.get_signing_key_from_jwt(credentials.credentials)
            payload = jwt.decode(credentials.credentials, signing_key.key, algorithms=["RS256"])
            current_user = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
        except Exception:
            pass

    now = datetime.now(timezone.utc)
    query = (
        db.query(Post)
        .filter(
            Post.is_removed == False,
            or_(
                func.lower(Post.title).contains(term),
                func.lower(Post.body).contains(term),
            ),
            or_(
                Post.subscriber_only == False,
                Post.author_id == current_user.id if current_user else False,
                Post.community_id.in_(
                    db.query(CommunitySubscription.community_id).filter(
                        CommunitySubscription.subscriber_user_id == current_user.id if current_user else None,
                        CommunitySubscription.status == "active",
                        CommunitySubscription.current_period_end > now,
                    )
                ) if current_user else False,
            )
        )
    )

    if community:
        comm = db.query(Community).filter(Community.name == community).first()
        if comm:
            query = query.filter(Post.community_id == comm.id)

    if sort == "new":
        query = query.order_by(desc(Post.created_at))
    elif sort == "top":
        query = query.order_by(desc(Post.upvotes - Post.downvotes))
    else:
        # relevance: title match weighted higher than body match
        query = query.order_by(
            func.lower(Post.title).contains(term).desc(),
            desc(Post.upvotes - Post.downvotes),
            desc(Post.created_at),
        )

    results = query.offset(offset).limit(limit).all()

    return {
        "query": q,
        "posts": [format_post(p) for p in results],
        "count": len(results),
    }


# ── User search ────────────────────────────────────────────────────────────────

@router.get("/users")
def search_users(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, le=30),
    offset: int = Query(0),
    db: Session = Depends(get_db),
):
    """
    Search users by username or display_name.
    Used by: global search, mention autocomplete.
    Excludes banned users.
    """
    term = q.strip().lower()

    results = (
        db.query(User)
        .filter(
            User.is_banned == False,
            User.username.isnot(None),
            or_(
                func.lower(User.username).contains(term),
                func.lower(User.display_name).contains(term),
            )
        )
        .order_by(
            (func.lower(User.username) == term).desc(),
            func.lower(User.username).startswith(term).desc(),
            desc(User.reputation),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "query": q,
        "users": [format_user(u) for u in results],
        "count": len(results),
    }


# ── Global search ──────────────────────────────────────────────────────────────

@router.get("/")
def global_search(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    """
    Global search returning top results across communities, posts, and users.
    Used by: navbar search.
    Returns limited results per category for speed.
    """
    term = q.strip().lower()

    # Communities — top 5
    communities = (
        db.query(Community)
        .filter(
            Community.is_private == False,
            or_(
                func.lower(Community.name).contains(term),
                func.lower(Community.display_name).contains(term),
            )
        )
        .order_by(
            (func.lower(Community.name) == term).desc(),
            desc(Community.member_count),
        )
        .limit(5)
        .all()
    )

    # Posts — top 5 (exclude subscriber-only)
    posts = (
        db.query(Post)
        .filter(
            Post.is_removed == False,
            Post.subscriber_only == False,
            or_(
                func.lower(Post.title).contains(term),
                func.lower(Post.body).contains(term),
            )
        )
        .order_by(
            func.lower(Post.title).contains(term).desc(),
            desc(Post.upvotes),
        )
        .limit(5)
        .all()
    )

    # Users — top 3
    users = (
        db.query(User)
        .filter(
            User.is_banned == False,
            User.username.isnot(None),
            or_(
                func.lower(User.username).contains(term),
                func.lower(User.display_name).contains(term),
            )
        )
        .order_by(
            (func.lower(User.username) == term).desc(),
            desc(User.reputation),
        )
        .limit(3)
        .all()
    )

    return {
        "query": q,
        "communities": [format_community(c) for c in communities],
        "posts": [format_post(p) for p in posts],
        "users": [format_user(u) for u in users],
    }