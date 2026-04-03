from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import desc, func, or_
from typing import Optional

from app.core.deps import get_db
from app.models.post import Post
from app.models.community import Community
from app.models.user import User
from app.api.routes.posts import format_post

router = APIRouter(prefix="/feeds/explore", tags=["feeds"])
optional_security = HTTPBearer(auto_error=False)


def _resolve_user(credentials, db: Session) -> User | None:
    if not credentials:
        return None
    try:
        from jwt import PyJWKClient
        import jwt, os
        jwk_client = PyJWKClient(f"https://{os.getenv('CLERK_FRONTEND_API')}/.well-known/jwks.json")
        signing_key = jwk_client.get_signing_key_from_jwt(credentials.credentials)
        payload = jwt.decode(credentials.credentials, signing_key.key, algorithms=["RS256"])
        return db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    except Exception:
        return None


@router.get("/posts")
def explore_posts(
    q: str = Query(""),
    community: Optional[str] = Query(None),
    sort: str = Query("relevance"),     # relevance | new | top
    limit: int = Query(30, le=50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
):
    """
    Explore posts algorithm:
    - Full text search on title + body
    - Always excludes subscriber-only (no subscription context)
    - Relevance: title match weighted above body match, then by votes
    """
    current_user = _resolve_user(credentials, db)

    query = db.query(Post).filter(
        Post.is_removed == False,
        Post.subscriber_only == False,
    )

    if not current_user or not getattr(current_user, 'show_nsfw', False):
        query = query.filter(Post.is_nsfw == False)

    if community:
        comm = db.query(Community).filter(Community.name == community).first()
        if comm:
            query = query.filter(Post.community_id == comm.id)

    if q.strip():
        term = q.strip().lower()
        query = query.filter(
            or_(
                func.lower(Post.title).contains(term),
                func.lower(Post.body).contains(term),
            )
        )
        if sort == "relevance":
            query = query.order_by(
                func.lower(Post.title).contains(term).desc(),
                desc(Post.upvotes - Post.downvotes),
                desc(Post.created_at),
            )
        elif sort == "new":
            query = query.order_by(desc(Post.created_at))
        else:
            query = query.order_by(desc(Post.upvotes - Post.downvotes))
    else:
        query = query.order_by(desc(Post.upvotes - Post.downvotes), desc(Post.created_at))

    posts = query.offset(offset).limit(limit).all()

    return {
        "posts": [format_post(p, current_user_id=current_user.id if current_user else None, db=db) for p in posts],
        "count": len(posts),
        "query": q,
    }


@router.get("/communities")
def explore_communities(
    q: str = Query(""),
    category: Optional[str] = Query(None),
    sort: str = Query("members"),       # members | new
    limit: int = Query(20, le=50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
):
    """
    Explore communities algorithm:
    - Optional text search on name + description
    - Optional category filter
    - Sorted by member count or newest
    """
    query = db.query(Community).filter(Community.is_private == False)

    if q.strip():
        term = q.strip().lower()
        query = query.filter(
            or_(
                func.lower(Community.name).contains(term),
                func.lower(Community.display_name).contains(term),
                func.lower(Community.description).contains(term),
            )
        )

    if category:
        query = query.filter(Community.category == category)

    if sort == "new":
        query = query.order_by(desc(Community.created_at))
    else:
        query = query.order_by(desc(Community.member_count))

    communities = query.offset(offset).limit(limit).all()

    return {
        "communities": [
            {
                "id": str(c.id),
                "name": c.name,
                "display_name": c.display_name,
                "description": c.description,
                "icon_url": c.icon_url,
                "banner_url": c.banner_url,
                "member_count": c.member_count,
                "category": c.category,
                "is_nsfw": c.is_nsfw,
            }
            for c in communities
        ],
        "count": len(communities),
        "query": q,
    }