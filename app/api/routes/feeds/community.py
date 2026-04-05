
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Literal, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jwt import PyJWKClient
from sqlalchemy import desc, func, or_, select, exists
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_db
from app.models.community import Community
from app.models.community_member import CommunityMember
from app.models.community_subscription import CommunitySubscription
from app.models.post import Post
from app.models.user import User
from app.api.routes.posts import format_post

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feeds/community", tags=["feeds"])
optional_security = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Shared JWK client singleton
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_jwk_client() -> PyJWKClient:
    frontend_api = os.getenv("CLERK_FRONTEND_API")
    if not frontend_api:
        raise RuntimeError("CLERK_FRONTEND_API env var is not set")
    return PyJWKClient(f"https://{frontend_api}/.well-known/jwks.json")


def _resolve_user(
    credentials: Optional[HTTPAuthorizationCredentials],
    db: Session,
) -> User | None:
    if not credentials:
        return None
    try:
        client = _get_jwk_client()
        signing_key = client.get_signing_key_from_jwt(credentials.credentials)
        payload = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=["RS256"],
        )
        return db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    except Exception:
        logger.debug("JWT decode failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Role checks — two scalar EXISTS queries against indexed FK columns
# ---------------------------------------------------------------------------

def _resolve_roles(
    community_id,
    user: User | None,
    db: Session,
) -> tuple[bool, bool]:
    """
    Returns (is_mod, is_subscribed).
    Two lightweight EXISTS scalar queries — no row hydration.
    Returns (False, False) immediately when user is None.
    """
    if user is None:
        return False, False

    now = datetime.now(timezone.utc)

    is_mod = db.query(
        exists(
            select(CommunityMember.id).where(
                CommunityMember.community_id == community_id,
                CommunityMember.user_id == user.id,
                CommunityMember.is_moderator.is_(True),
            )
        )
    ).scalar()

    is_sub = db.query(
        exists(
            select(CommunitySubscription.id).where(
                CommunitySubscription.community_id == community_id,
                CommunitySubscription.subscriber_user_id == user.id,
                CommunitySubscription.status.in_(["active", "past_due"]),
                CommunitySubscription.current_period_end > now,
            )
        )
    ).scalar()

    return bool(is_mod), bool(is_sub)


# ---------------------------------------------------------------------------
# SQL hot-score expression
#   score = (net_votes + comment_boost) / (age_hours + 2) ^ 1.5
# ---------------------------------------------------------------------------

def _hot_score_expr():
    age_hours = func.greatest(
        func.extract("epoch", func.now() - Post.created_at) / 3600.0,
        0.1,
    )
    net_votes = Post.upvotes - Post.downvotes
    return (net_votes * 1.0 + Post.comment_count * 0.2) / func.pow(age_hours + 2, 1.5)


# ---------------------------------------------------------------------------
# Community feed endpoint
# ---------------------------------------------------------------------------

@router.get("/{name}")
def community_feed(
    name: str,
    sort: Literal["new", "top", "hot"] = Query("new"),
    feed: Literal["all", "subscribers"] = Query("all"),
    limit: int = Query(30, ge=1, le=50),
    # Cursor shapes:
    #   new  → "<created_at_iso>:<post_id>"       ← FIX 1: now includes id
    #   top  → "<net_votes>:<post_id>"
    #   hot  → "<score_float>:<post_id>"
    after_cursor: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
):
    # ------------------------------------------------------------------
    # Resolve community
    # ------------------------------------------------------------------
    community = db.query(Community).filter(Community.name == name).first()
    if not community:
        raise HTTPException(status_code=404, detail="Community not found")

    # ------------------------------------------------------------------
    # Resolve user + roles
    # ------------------------------------------------------------------
    current_user = _resolve_user(credentials, db)
    is_mod, is_sub = _resolve_roles(community.id, current_user, db)
    can_see_subscriber_only = is_mod or is_sub

    # ------------------------------------------------------------------
    # Base query — joinedload kills N+1 inside format_post
    # ------------------------------------------------------------------
    query = (
        db.query(Post)
        .options(
            joinedload(Post.author),
            joinedload(Post.community),
        )
        .filter(
            Post.community_id == community.id,
            Post.is_removed.is_(False),
        )
    )

    # NSFW gate
    if not current_user or not getattr(current_user, "show_nsfw", False):
        query = query.filter(Post.is_nsfw.is_(False))

    # ------------------------------------------------------------------
    # FIX 2: Subscriber-only visibility — clean separation of tabs
    #
    # "all"         → public posts only, ALWAYS. Mods and subscribers see
    #                 the same content here as everyone else. Subscriber-only
    #                 posts belong exclusively in the subscribers tab.
    #
    # "subscribers" → subscriber-only posts only. Gated: only mods and
    #                 active subscribers may access. Others get an early
    #                 return with subscription_required=True so the frontend
    #                 can render an upgrade prompt.
    # ------------------------------------------------------------------
    if feed == "subscribers":
        query = query.filter(Post.subscriber_only.is_(True))
        if not can_see_subscriber_only:
            return {
                "posts": [],
                "count": 0,
                "next_cursor": None,
                "is_subscribed": is_sub,
                "is_mod": is_mod,
                "subscription_required": True,
            }
    else:
        # "all" tab: subscriber-only posts are always excluded, no exceptions
        query = query.filter(Post.subscriber_only.is_(False))

    # ------------------------------------------------------------------
    # Sort + cursor
    # ------------------------------------------------------------------

    if sort == "new":
        # FIX 1: two-part cursor — "<created_at_iso>:<post_id>"
        # Stable even when multiple posts share the same created_at.
        if after_cursor:
            try:
                ts_str, last_id = after_cursor.split(":", 1)
                cursor_dt = datetime.fromisoformat(ts_str)
                if cursor_dt.tzinfo is None:
                    cursor_dt = cursor_dt.replace(tzinfo=timezone.utc)
                query = query.filter(
                    or_(
                        Post.created_at < cursor_dt,
                        (Post.created_at == cursor_dt) & (Post.id < last_id),
                    )
                )
            except (ValueError, AttributeError):
                raise HTTPException(status_code=400, detail="Invalid cursor for sort=new")

        posts = (
            query
            .order_by(desc(Post.created_at), desc(Post.id))
            .limit(limit)
            .all()
        )
        if posts:
            last = posts[-1]
            next_cursor = f"{last.created_at.isoformat()}:{last.id}"
        else:
            next_cursor = None

    elif sort == "top":
        net_votes = Post.upvotes - Post.downvotes

        if after_cursor:
            try:
                score_str, last_id = after_cursor.split(":", 1)
                cursor_score = int(score_str)
                query = query.filter(
                    or_(
                        net_votes < cursor_score,
                        (net_votes == cursor_score) & (Post.id < last_id),
                    )
                )
            except (ValueError, AttributeError):
                raise HTTPException(status_code=400, detail="Invalid cursor for sort=top")

        posts = (
            query
            .order_by(desc(net_votes), desc(Post.created_at), desc(Post.id))
            .limit(limit)
            .all()
        )
        if posts:
            last = posts[-1]
            next_cursor = f"{last.upvotes - last.downvotes}:{last.id}"
        else:
            next_cursor = None

    else:
        # ----------------------------------------------------------------
        # Hot sort
        # ----------------------------------------------------------------
        hot_score = _hot_score_expr().label("hot_score")
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)

        recent_q = (
            query
            .filter(Post.created_at > cutoff)
            .add_columns(hot_score)
        )

        if after_cursor:
            try:
                score_str, last_id = after_cursor.split(":", 1)
                cursor_score = float(score_str)
                recent_q = recent_q.filter(
                    or_(
                        hot_score < cursor_score,
                        (hot_score == cursor_score) & (Post.id < last_id),
                    )
                )
            except (ValueError, AttributeError):
                raise HTTPException(status_code=400, detail="Invalid cursor for sort=hot")

        recent_rows = (
            recent_q
            .order_by(desc("hot_score"), desc(Post.created_at), desc(Post.id))
            .limit(limit)
            .all()
        )

        posts = [row[0] if isinstance(row, tuple) else row for row in recent_rows]
        scores = [row[1] if isinstance(row, tuple) else None for row in recent_rows]

        # Pad with best older posts if recent window is sparse
        if len(posts) < limit:
            seen_ids = {p.id for p in posts}
            older = (
                query
                .filter(
                    Post.created_at <= cutoff,
                    ~Post.id.in_(seen_ids) if seen_ids else True,
                )
                .order_by(
                    desc(Post.upvotes - Post.downvotes),
                    desc(Post.created_at),
                    desc(Post.id),
                )
                .limit(limit - len(posts))
                .all()
            )
            posts += older
            scores += [None] * len(older)

        # Cursor uses DB-returned score; Python fallback only for padded
        # older-post rows (always last on the page, never mid-page).
        if posts:
            last = posts[-1]
            last_score = scores[-1]
            if last_score is None:
                created = last.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_h = max(
                    (datetime.now(timezone.utc) - created).total_seconds() / 3600,
                    0.1,
                )
                last_score = (
                    (last.upvotes - last.downvotes * 1.0 + last.comment_count * 0.2) / (age_h + 2) ** 1.5
                )
            next_cursor = f"{last_score:.6f}:{last.id}"
        else:
            next_cursor = None

    # ------------------------------------------------------------------
    # Serialise
    # ------------------------------------------------------------------
    user_id = current_user.id if current_user else None
    return {
        "posts": [format_post(p, current_user_id=user_id, db=db) for p in posts],
        "count": len(posts),
        "next_cursor": next_cursor,
        "is_subscribed": is_sub,
        "is_mod": is_mod,
        "subscription_required": False,
    }