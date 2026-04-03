from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import Optional
from datetime import datetime, timezone, timedelta

from app.core.deps import get_db
from app.models.post import Post
from app.models.community import Community
from app.models.community_subscription import CommunitySubscription
from app.models.community_member import CommunityMember
from app.models.user import User
from app.api.routes.posts import format_post

router = APIRouter(prefix="/feeds/popular", tags=["feeds"])
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


@router.get("/")
def popular_feed(
    window: str = Query("week"),    # week | month | all
    limit: int = Query(50, le=100),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
):
    """
    Popular feed algorithm:
    - Ranked by net votes (upvotes - downvotes)
    - Time windowed: week / month / all time
    - Always excludes subscriber-only posts (no subscription context here)
    - Excludes NSFW unless user has opted in
    """
    current_user = _resolve_user(credentials, db)

    query = db.query(Post).filter(
        Post.is_removed == False,
        Post.subscriber_only == False,     # never show subscriber-only in popular
    )

    if not current_user or not getattr(current_user, 'show_nsfw', False):
        query = query.filter(Post.is_nsfw == False)

    now = datetime.now(timezone.utc)
    if window == "week":
        query = query.filter(Post.created_at > now - timedelta(days=7))
    elif window == "month":
        query = query.filter(Post.created_at > now - timedelta(days=30))
    # "all" — no time filter

    posts = query.order_by(
        desc(Post.upvotes - Post.downvotes),
        desc(Post.created_at),
    ).offset(offset).limit(limit).all()

    # Also return top communities by member count for the sidebar
    top_communities = (
        db.query(Community)
        .filter(Community.is_private == False)
        .order_by(desc(Community.member_count))
        .limit(5)
        .all()
    )

    return {
        "posts": [format_post(p, current_user_id=current_user.id if current_user else None, db=db) for p in posts],
        "count": len(posts),
        "window": window,
        "top_communities": [
            {"name": c.name, "display_name": c.display_name, "icon_url": c.icon_url, "member_count": c.member_count}
            for c in top_communities
        ],
    }