"""
feeds/home.py — Production-hardened home feed (v2)
===================================================
Fixes over v1:
1.  Hot cursor now uses DB-returned score + Post.id (no Python recomputation)
2.  .having() replaced with .filter() on labelled expression — no GROUP BY needed
3.  Tie-breakers on every sort: (primary, created_at DESC, id DESC)
4.  literal(now_utc) replaced with func.now() for safe query plan caching
5.  Post.id.notin_() replaced with ~Post.id.in_()
6.  Top feed cursor uses (score, id) pair to prevent duplicates at equal scores
7.  Unused imports removed (case, and_, Community)
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jwt import PyJWKClient
from sqlalchemy import func, desc, or_, exists, select
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_db
from app.models.community_member import CommunityMember
from app.models.community_subscription import CommunitySubscription
from app.models.post import Post
from app.models.user import User
from app.api.routes.posts import format_post

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feeds/home", tags=["feeds"])
optional_security = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Shared JWK client — initialised once per process, not per request
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_jwk_client() -> PyJWKClient:
    frontend_api = os.getenv("CLERK_FRONTEND_API")
    if not frontend_api:
        raise RuntimeError("CLERK_FRONTEND_API env var is not set")
    return PyJWKClient(f"https://{frontend_api}/.well-known/jwks.json")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _resolve_user(
    credentials: Optional[HTTPAuthorizationCredentials],
    db: Session,
) -> User | None:
    """Decode Clerk JWT and return the matching DB user, or None."""
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
        return (
            db.query(User)
            .filter(User.clerk_user_id == payload["sub"])
            .first()
        )
    except Exception:
        logger.debug("JWT decode failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# SQL hot-score expression
# ---------------------------------------------------------------------------

def _hot_score_expr():
    """
    score = (net_votes + comment_boost) / (age_hours + 2) ^ 1.5

    FIX v2: uses func.now() instead of literal(datetime.now(utc)) so the
    expression is stable for query plan caching across requests.
    """
    # FIX 4: func.now() instead of literal(now_utc)
    age_hours = func.greatest(
        func.extract(
            "epoch",
            func.now() - Post.created_at,
        ) / 3600.0,
        0.1,
    )
    net_votes = Post.upvotes - Post.downvotes
    comment_boost = Post.comment_count * 0.5

    return (net_votes + comment_boost) / func.pow(age_hours + 2, 1.5)


# ---------------------------------------------------------------------------
# Visibility filter
# ---------------------------------------------------------------------------

def _apply_visibility(query, current_user: User | None, db: Session):
    """
    Filter out subscriber-only posts the requester has no right to see.
    Uses correlated EXISTS subqueries to avoid large IN(...) lists.
    """
    if current_user is None:
        return query.filter(Post.subscriber_only.is_(False))

    now = datetime.now(timezone.utc)

    is_mod = exists(
        select(CommunityMember.id).where(
            CommunityMember.community_id == Post.community_id,
            CommunityMember.user_id == current_user.id,
            CommunityMember.is_moderator.is_(True),
        )
    )

    is_subscriber = exists(
        select(CommunitySubscription.id).where(
            CommunitySubscription.community_id == Post.community_id,
            CommunitySubscription.subscriber_user_id == current_user.id,
            CommunitySubscription.status.in_(["active", "past_due"]),
            CommunitySubscription.current_period_end > now,
        )
    )

    return query.filter(
        or_(
            Post.subscriber_only.is_(False),
            Post.author_id == current_user.id,
            is_mod,
            is_subscriber,
        )
    )


# ---------------------------------------------------------------------------
# Feed endpoint
# ---------------------------------------------------------------------------

@router.get("/")
def home_feed(
    sort: str = Query("hot", pattern="^(hot|new|top)$"),
    limit: int = Query(20, ge=1, le=50),
    after_cursor: Optional[str] = Query(None, description="Opaque pagination cursor"),
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
):
    """
    Home feed — hot / new / top with cursor-based pagination.

    Cursor shapes:
        new  → ISO datetime string  (created_at of last item)
        top  → "<net_votes>:<post_id>"
        hot  → "<score_float>:<post_id>"

    Required DB indexes:
        CREATE INDEX idx_posts_created_at      ON posts (created_at DESC);
        CREATE INDEX idx_posts_removed_nsfw    ON posts (is_removed, is_nsfw);
        CREATE INDEX idx_posts_community_id    ON posts (community_id);
        CREATE INDEX idx_posts_upvotes_down    ON posts (upvotes, downvotes);
        CREATE INDEX idx_posts_subscriber_only ON posts (subscriber_only);
    """
    current_user = _resolve_user(credentials, db)

    # Base query — eager-load to kill N+1 inside format_post
    query = (
        db.query(Post)
        .options(
            joinedload(Post.author),
            joinedload(Post.community),
        )
        .filter(Post.is_removed.is_(False))
    )

    # NSFW gate
    if not current_user or not getattr(current_user, "show_nsfw", False):
        query = query.filter(Post.is_nsfw.is_(False))

    # Subscriber-only visibility gate
    query = _apply_visibility(query, current_user, db)

    # ------------------------------------------------------------------
    # Sort + cursor
    # ------------------------------------------------------------------

    if sort == "new":
        if after_cursor:
            try:
                cursor_dt = datetime.fromisoformat(after_cursor)
                if cursor_dt.tzinfo is None:
                    cursor_dt = cursor_dt.replace(tzinfo=timezone.utc)
                query = query.filter(Post.created_at < cursor_dt)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid cursor for sort=new")

        posts = (
            query
            # FIX 3: tie-break with id so equal timestamps are stable
            .order_by(desc(Post.created_at), desc(Post.id))
            .limit(limit)
            .all()
        )
        next_cursor = posts[-1].created_at.isoformat() if posts else None

    elif sort == "top":
        net_votes = (Post.upvotes - Post.downvotes).label("net_votes")

        if after_cursor:
            try:
                # FIX 6: cursor carries both score and id to handle ties
                score_str, last_id = after_cursor.split(":", 1)
                cursor_score = int(score_str)
                query = query.filter(
                    or_(
                        (Post.upvotes - Post.downvotes) < cursor_score,
                        # same score → fall back to id ordering
                        ((Post.upvotes - Post.downvotes) == cursor_score)
                        & (Post.id < last_id),
                    )
                )
            except (ValueError, AttributeError):
                raise HTTPException(status_code=400, detail="Invalid cursor for sort=top")

        posts = (
            query
            # FIX 3: full tie-break chain
            .order_by(desc(Post.upvotes - Post.downvotes), desc(Post.created_at), desc(Post.id))
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
                # FIX 1 + 2: cursor = "<score>:<id>"; filter in WHERE not HAVING
                score_str, last_id = after_cursor.split(":", 1)
                cursor_score = float(score_str)
                # FIX 2: .filter() on the labelled column — no GROUP BY needed
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
            # FIX 3: tie-break chain on hot sort
            .order_by(desc("hot_score"), desc(Post.created_at), desc(Post.id))
            .limit(limit)
            .all()
        )

        # recent_rows → list of (Post, score) tuples when add_columns is used
        posts = [row[0] if isinstance(row, tuple) else row for row in recent_rows]
        scores = [row[1] if isinstance(row, tuple) else None for row in recent_rows]

        # Pad with best older posts if recent window is sparse
        if len(posts) < limit:
            # FIX 5: ~Post.id.in_() instead of Post.id.notin_()
            seen_ids = {p.id for p in posts}
            older_posts = (
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
            posts += older_posts
            scores += [None] * len(older_posts)

        # FIX 1: cursor uses DB-returned score, falls back to Python only
        # for the older-post pad rows (which are appended last, so cursor
        # won't be requested mid-pad in normal usage).
        if posts:
            last = posts[-1]
            last_score = scores[-1]
            if last_score is None:
                # Older-post pad row — recompute in Python (acceptable; rare path)
                created = last.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_h = max(
                    (datetime.now(timezone.utc) - created).total_seconds() / 3600,
                    0.1,
                )
                last_score = (
                    (last.upvotes - last.downvotes + last.comment_count * 0.5)
                    / (age_h + 2) ** 1.5
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
    }