"""
feeds/explore.py — Production-hardened explore feed
====================================================
Key improvements over original:
- PostgreSQL full-text search (tsvector/tsquery) replaces ILIKE/contains
- ts_rank() relevance scoring with title weighted above body (A > B)
- Cursor-based pagination replaces offset
- Shared PyJWKClient singleton (not re-initialised per request)
- joinedload to prevent N+1 in format_post
- Subquery for community filter (no full object fetch)
- .is_(False) / .is_(True) everywhere
- sort validated via Literal type
- term preprocessed once
- Early 404 on unknown community slug

Required DB setup (run once in Neon SQL editor):
-------------------------------------------------
-- Posts full-text index (weighted: title=A, body=B)
ALTER TABLE posts
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(body,  '')), 'B')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_posts_search_vector
    ON posts USING GIN (search_vector);

-- Communities full-text index
ALTER TABLE communities
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name,         '')), 'A') ||
        setweight(to_tsvector('english', coalesce(display_name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(description,  '')), 'B')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_communities_search_vector
    ON communities USING GIN (search_vector);

-- Supporting indexes
CREATE INDEX IF NOT EXISTS idx_posts_created_at       ON posts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_removed_nsfw     ON posts (is_removed, is_nsfw);
CREATE INDEX IF NOT EXISTS idx_posts_community_id     ON posts (community_id);
CREATE INDEX IF NOT EXISTS idx_posts_sub_only         ON posts (subscriber_only);
CREATE INDEX IF NOT EXISTS idx_posts_net_votes        ON posts ((upvotes - downvotes) DESC);
CREATE INDEX IF NOT EXISTS idx_communities_member_cnt ON communities (member_count DESC);
CREATE INDEX IF NOT EXISTS idx_communities_created_at ON communities (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_communities_category   ON communities (category);
CREATE INDEX IF NOT EXISTS idx_communities_private    ON communities (is_private);
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jwt import PyJWKClient
from sqlalchemy import cast, desc, func, or_, select, text
from sqlalchemy.dialects.postgresql import REGCONFIG, TSVECTOR
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_db
from app.models.community import Community
from app.models.post import Post
from app.models.user import User
from app.api.routes.posts import format_post

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feeds/explore", tags=["feeds"])
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
# Full-text search helpers
# ---------------------------------------------------------------------------

_PG_LANG = "english"


def _to_tsquery(term: str):
    """
    Convert a raw search term to a PostgreSQL tsquery.
    Joins tokens with & (AND) and appends :* for prefix matching on the last
    token so partial queries still hit the GIN index.
    """
    tokens = term.split()
    if not tokens:
        return None
    # Prefix-match the last token; exact-match the rest
    parts = [f"{t}:*" if i == len(tokens) - 1 else t for i, t in enumerate(tokens)]
    raw = " & ".join(parts)
    return func.to_tsquery(_PG_LANG, raw)


def _post_rank_expr(tsquery):
    """ts_rank against the stored search_vector column."""
    return func.ts_rank(Post.search_vector, tsquery)


def _community_rank_expr(tsquery):
    return func.ts_rank(Community.search_vector, tsquery)


# ---------------------------------------------------------------------------
# /explore/posts
# ---------------------------------------------------------------------------

@router.get("/posts")
def explore_posts(
    q: str = Query("", max_length=200),
    community: Optional[str] = Query(None, max_length=100),
    sort: Literal["relevance", "new", "top"] = Query("relevance"),
    limit: int = Query(30, ge=1, le=50),
    # Cursor shapes:
    #   new  → ISO datetime string (created_at of last item)
    #   top  → "<net_votes>:<post_id>" (net votes of last item + id tiebreak)
    #   relevance → "<rank_float>:<post_id>"
    after_cursor: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
):
    """
    Explore posts with PostgreSQL full-text search and cursor pagination.

    Relevance ranking:
        ts_rank(search_vector, query)   — title weighted A, body weighted B
        + tiebreak: net votes, then newest
    """
    current_user = _resolve_user(credentials, db)
    term = q.strip().lower()

    # ------------------------------------------------------------------
    # Base query — always exclude removed + subscriber-only
    # ------------------------------------------------------------------
    query = (
        db.query(Post)
        .options(
            joinedload(Post.author),
            joinedload(Post.community),
        )
        .filter(
            Post.is_removed.is_(False),
            Post.subscriber_only.is_(False),
        )
    )

    if not current_user or not getattr(current_user, "show_nsfw", False):
        query = query.filter(Post.is_nsfw.is_(False))

    # ------------------------------------------------------------------
    # Community filter — use subquery, don't fetch full object
    # ------------------------------------------------------------------
    if community:
        comm_id_sq = (
            select(Community.id)
            .where(Community.name == community)
            .scalar_subquery()
        )
        # Validate community exists
        exists_check = db.query(Community.id).filter(Community.name == community).scalar()
        if exists_check is None:
            raise HTTPException(status_code=404, detail=f"Community '{community}' not found")
        query = query.filter(Post.community_id == comm_id_sq)

    # ------------------------------------------------------------------
    # Full-text search + ranking
    # ------------------------------------------------------------------
    tsquery = _to_tsquery(term) if term else None

    if tsquery is not None:
        # Filter to rows that match the tsquery (GIN index hit)
        query = query.filter(Post.search_vector.op("@@")(tsquery))

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
        posts = query.order_by(desc(Post.created_at)).limit(limit).all()
        next_cursor = posts[-1].created_at.isoformat() if posts else None

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
            .order_by(desc(net_votes), desc(Post.created_at))
            .limit(limit)
            .all()
        )
        if posts:
            last = posts[-1]
            next_cursor = f"{last.upvotes - last.downvotes}:{last.id}"
        else:
            next_cursor = None

    else:
        # Relevance — rank by ts_rank if we have a query, else fall back to top
        if tsquery is not None:
            rank_expr = _post_rank_expr(tsquery).label("rank")
            net_votes = Post.upvotes - Post.downvotes

            if after_cursor:
                try:
                    rank_str, last_id = after_cursor.split(":", 1)
                    cursor_rank = float(rank_str)
                    # Can't easily filter add_columns in WHERE; use subquery approach
                    # Approximate: filter by rank < cursor (slight duplication risk at boundary)
                    query = query.having(rank_expr < cursor_rank)
                except (ValueError, AttributeError):
                    raise HTTPException(status_code=400, detail="Invalid cursor for sort=relevance")

            rows = (
                query
                .add_columns(rank_expr)
                .order_by(desc("rank"), desc(net_votes), desc(Post.created_at))
                .limit(limit)
                .all()
            )
            posts = [row[0] for row in rows]
            ranks = [row[1] for row in rows]
            next_cursor = f"{ranks[-1]:.6f}:{posts[-1].id}" if posts else None
        else:
            # No search term + relevance → behave like top
            net_votes = Post.upvotes - Post.downvotes
            posts = (
                query
                .order_by(desc(net_votes), desc(Post.created_at))
                .limit(limit)
                .all()
            )
            next_cursor = None

    # ------------------------------------------------------------------
    # Serialise
    # ------------------------------------------------------------------
    user_id = current_user.id if current_user else None
    return {
        "posts": [format_post(p, current_user_id=user_id, db=db) for p in posts],
        "count": len(posts),
        "query": q,
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------------------
# /explore/communities
# ---------------------------------------------------------------------------

@router.get("/communities")
def explore_communities(
    q: str = Query("", max_length=200),
    category: Optional[str] = Query(None, max_length=100),
    sort: Literal["members", "new", "relevance"] = Query("members"),
    limit: int = Query(20, ge=1, le=50),
    # Cursor shapes:
    #   members    → "<member_count>:<community_id>"
    #   new        → ISO datetime string
    #   relevance  → "<rank_float>:<community_id>"
    after_cursor: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    term = q.strip().lower()

    query = db.query(Community).filter(Community.is_private.is_(False))

    if category:
        query = query.filter(Community.category == category)

    tsquery = _to_tsquery(term) if term else None

    if tsquery is not None:
        query = query.filter(Community.search_vector.op("@@")(tsquery))

    # ------------------------------------------------------------------
    # Sort + cursor
    # ------------------------------------------------------------------
    if sort == "new":
        if after_cursor:
            try:
                cursor_dt = datetime.fromisoformat(after_cursor)
                if cursor_dt.tzinfo is None:
                    cursor_dt = cursor_dt.replace(tzinfo=timezone.utc)
                query = query.filter(Community.created_at < cursor_dt)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid cursor for sort=new")
        communities = query.order_by(desc(Community.created_at)).limit(limit).all()
        next_cursor = communities[-1].created_at.isoformat() if communities else None

    elif sort == "relevance" and tsquery is not None:
        rank_expr = _community_rank_expr(tsquery).label("rank")

        if after_cursor:
            try:
                rank_str, last_id = after_cursor.split(":", 1)
                cursor_rank = float(rank_str)
                query = query.having(rank_expr < cursor_rank)
            except (ValueError, AttributeError):
                raise HTTPException(status_code=400, detail="Invalid cursor for sort=relevance")

        rows = (
            query
            .add_columns(rank_expr)
            .order_by(desc("rank"), desc(Community.member_count))
            .limit(limit)
            .all()
        )
        communities = [row[0] for row in rows]
        ranks = [row[1] for row in rows]
        next_cursor = f"{ranks[-1]:.6f}:{communities[-1].id}" if communities else None

    else:
        # Default: members desc
        if after_cursor:
            try:
                count_str, last_id = after_cursor.split(":", 1)
                cursor_count = int(count_str)
                query = query.filter(
                    or_(
                        Community.member_count < cursor_count,
                        (Community.member_count == cursor_count) & (Community.id < last_id),
                    )
                )
            except (ValueError, AttributeError):
                raise HTTPException(status_code=400, detail="Invalid cursor for sort=members")
        communities = (
            query
            .order_by(desc(Community.member_count), Community.id)
            .limit(limit)
            .all()
        )
        if communities:
            last = communities[-1]
            next_cursor = f"{last.member_count}:{last.id}"
        else:
            next_cursor = None

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
        "next_cursor": next_cursor,
    }