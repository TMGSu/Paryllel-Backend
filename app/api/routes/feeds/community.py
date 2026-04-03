from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Optional
from datetime import datetime, timezone

from app.core.deps import get_db
from app.models.post import Post
from app.models.community import Community
from app.models.community_subscription import CommunitySubscription
from app.models.community_member import CommunityMember
from app.models.user import User
from app.api.routes.posts import format_post
from app.services import subscription_service

router = APIRouter(prefix="/feeds/community", tags=["feeds"])
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


@router.get("/{name}")
def community_feed(
    name: str,
    sort: str = Query("new"),       # new | top
    feed: str = Query("all"),       # all | subscribers
    limit: int = Query(30, le=50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
):
    community = db.query(Community).filter(Community.name == name).first()
    if not community:
        raise HTTPException(status_code=404, detail="Community not found")

    current_user = _resolve_user(credentials, db)

    # Check mod status
    is_mod = False
    if current_user:
        mod = db.query(CommunityMember).filter(
            CommunityMember.community_id == community.id,
            CommunityMember.user_id == current_user.id,
            CommunityMember.is_moderator == True,
        ).first()
        is_mod = mod is not None

    # Check subscription
    is_sub = False
    if current_user and community.subscription_enabled:
        is_sub = subscription_service.is_subscribed(community.id, current_user.id, db)

    query = db.query(Post).filter(
        Post.is_removed == False,
        Post.community_id == community.id,
    )

    if community.subscription_enabled:
        if feed == "subscribers":
            # Subscribers feed: only subscriber_only posts
            # Mods and active subscribers see them; others get empty
            query = query.filter(Post.subscriber_only == True)
            if not is_mod and not is_sub:
                return {"posts": [], "count": 0, "is_subscribed": False}
        else:
            # All feed: hide subscriber_only from non-subscribers non-mods
            if not is_mod and not is_sub:
                query = query.filter(Post.subscriber_only == False)
    else:
        query = query.filter(Post.subscriber_only == False)

    if sort == "top":
        query = query.order_by(desc(Post.upvotes - Post.downvotes), desc(Post.created_at))
    else:
        query = query.order_by(desc(Post.created_at))

    posts = query.offset(offset).limit(limit).all()

    return {
        "posts": [format_post(p, current_user_id=current_user.id if current_user else None, db=db) for p in posts],
        "count": len(posts),
        "is_subscribed": is_sub,
        "is_mod": is_mod,
    }