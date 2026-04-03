from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Optional
from datetime import datetime, timezone, timedelta

from app.core.deps import get_db
from app.models.post import Post
from app.models.community import Community
from app.models.community_subscription import CommunitySubscription
from app.models.community_member import CommunityMember
from app.models.user import User
from app.api.routes.posts import format_post

router = APIRouter(prefix="/feeds/home", tags=["feeds"])
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


def _apply_visibility(query, current_user: User | None, db: Session):
    """Filter out subscriber-only posts the user can't see."""
    if not current_user:
        return query.filter(Post.subscriber_only == False)

    now = datetime.now(timezone.utc)
    mod_ids = db.query(CommunityMember.community_id).filter(
        CommunityMember.user_id == current_user.id,
        CommunityMember.is_moderator == True,
    )
    sub_ids = db.query(CommunitySubscription.community_id).filter(
        CommunitySubscription.subscriber_user_id == current_user.id,
        CommunitySubscription.status.in_(["active", "past_due"]),
        CommunitySubscription.current_period_end > now,
    )
    return query.filter(
        (Post.subscriber_only == False) |
        (Post.author_id == current_user.id) |
        (Post.community_id.in_(mod_ids)) |
        (Post.community_id.in_(sub_ids))
    )


def _hot_score(post: Post) -> float:
    """
    Hot score algorithm:
    score = (net_votes + comment_boost) / age_penalty
    - net_votes: upvotes minus downvotes
    - comment_boost: comments * 0.5 (engagement signal)
    - age_penalty: exponential decay over 48h
    """
    now = datetime.now(timezone.utc)
    created = post.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_hours = max((now - created).total_seconds() / 3600, 0.1)
    net_votes = post.upvotes - post.downvotes
    comment_boost = post.comment_count * 0.5
    return (net_votes + comment_boost) / (age_hours + 2) ** 1.5


@router.get("/")
def home_feed(
    sort: str = Query("hot"),   # hot | new | top
    limit: int = Query(20, le=50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
):
    current_user = _resolve_user(credentials, db)

    query = db.query(Post).filter(Post.is_removed == False)
    query = _apply_visibility(query, current_user, db)

    # NSFW filter
    if not current_user or not getattr(current_user, 'show_nsfw', False):
        query = query.filter(Post.is_nsfw == False)

    if sort == "new":
        posts = query.order_by(desc(Post.created_at)).offset(offset).limit(limit).all()
    elif sort == "top":
        posts = query.order_by(desc(Post.upvotes - Post.downvotes)).offset(offset).limit(limit).all()
    else:
        # Hot: pull last 7 days, rank by score, fall back to older if needed
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        candidates = query.filter(Post.created_at > cutoff).all()
        if len(candidates) < limit:
            seen_ids = {p.id for p in candidates}
            older = query.filter(
                Post.created_at <= cutoff,
                Post.id.notin_(seen_ids)
            ).order_by(desc(Post.upvotes - Post.downvotes)).limit(limit - len(candidates)).all()
            candidates += older
        posts = sorted(candidates, key=_hot_score, reverse=True)
        posts = posts[offset:offset + limit]

    return {
        "posts": [format_post(p, current_user_id=current_user.id if current_user else None, db=db) for p in posts],
        "count": len(posts),
    }