from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from typing import Optional, List
import uuid

from app.core.deps import get_db, get_db_with_clerk_id
from app.core.auth import verify_token
from app.models.post import Post
from app.models.community import Community
from app.models.user import User
from app.models.vote import Vote
from app.models.media import Media
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import re

router = APIRouter(prefix="/posts", tags=["posts"])


def generate_slug(title: str, post_id: str, db) -> str:
    slug = title.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s]+', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    slug = slug[:80]

    # Check if slug already exists
    existing = db.query(Post).filter(Post.slug == slug).first()
    if existing:
        # Only add suffix if there's a conflict
        short_id = str(post_id)[:8]
        slug = f"{slug}-{short_id}"

    return slug


class CreatePost(BaseModel):
    title: str
    body: Optional[str] = None
    community_name: str
    post_type: str = "text"  # text | image | video | link | poll
    tag: Optional[str] = None
    is_nsfw: bool = False
    link_url: Optional[str] = None
    media_urls: Optional[List[str]] = []
    poll_options: Optional[List[str]] = []


def format_post(post: Post, current_user_id=None, db: Session = None):
    result = {
        "id": str(post.id),
        "slug": post.slug,
        "title": post.title,
        "body": post.body,
        "post_type": post.post_type,
        "is_nsfw": post.is_nsfw,
        "is_pinned": post.is_pinned,
        "is_locked": post.is_locked,
        "upvotes": post.upvotes,
        "downvotes": post.downvotes,
        "score": post.upvotes - post.downvotes,
        "comment_count": post.comment_count,
        "tip_count": post.tip_count,
        "total_tips": post.total_tips,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "updated_at": post.updated_at.isoformat() if post.updated_at else None,
        "author": {
            "id": str(post.author.id),
            "username": post.author.username,
            "display_name": post.author.display_name,
            "avatar_url": post.author.avatar_url,
            "reputation": post.author.reputation,
        } if post.author else None,
        "community": {
            "name": post.community.name,
            "display_name": post.community.display_name,
            "icon_url": post.community.icon_url,
        } if post.community else None,
        "media": [{"url": m.url, "media_type": m.media_type} for m in post.media] if post.media else [],
        "user_vote": None,
    }

    # Get current user's vote if logged in
    if current_user_id and db:
        vote = db.query(Vote).filter(
            Vote.user_id == current_user_id,
            Vote.post_id == post.id
        ).first()
        result["user_vote"] = vote.value if vote else None

    return result



optional_security = HTTPBearer(auto_error=False)

@router.get("/")
def list_posts(
    community: Optional[str] = Query(None),
    sort: str = Query("hot"),
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
):
    query = db.query(Post).filter(Post.is_removed == False)

    if community:
        comm = db.query(Community).filter(Community.name == community).first()
        if comm:
            query = query.filter(Post.community_id == comm.id)

    if sort == "new":
        query = query.order_by(desc(Post.created_at))
    elif sort == "top":
        query = query.order_by(desc(Post.upvotes - Post.downvotes))
    else:
        query = query.order_by(desc(Post.upvotes - Post.downvotes), desc(Post.created_at))

    posts = query.offset(offset).limit(limit).all()

    # Try to get current user for vote state
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

    return {
        "posts": [format_post(p, current_user_id=current_user.id if current_user else None, db=db) for p in posts],
        "total": query.count()
    }

@router.post("/")
def create_post(
    body: CreatePost,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.username:
        raise HTTPException(status_code=400, detail="Complete your profile before posting")

    community = db.query(Community).filter(Community.name == body.community_name).first()
    if not community:
        raise HTTPException(status_code=404, detail="Community not found")

    if not body.title.strip():
        raise HTTPException(status_code=400, detail="Title is required")

    post = Post(
        author_id=user.id,
        community_id=community.id,
        title=body.title.strip(),
        body=body.body,
        post_type=body.post_type,
        is_nsfw=body.is_nsfw,
    )
    db.add(post)
    db.flush()

    post.slug = generate_slug(body.title.strip(), str(post.id), db)


    # Add media records
    for i, url in enumerate(body.media_urls or []):
        media_type = "video" if any(url.endswith(ext) for ext in [".mp4", ".webm", ".mov"]) else "image"
        media = Media(
            post_id=post.id,
            uploaded_by=user.id,
            url=url,
            media_type=media_type,
            sort_order=i,
        )
        db.add(media)

    db.commit()
    db.refresh(post)

    # Bump user reputation for posting
    user.reputation += 5
    db.commit()

    return {"message": "Post created", "post": format_post(post)}

@router.get("/slug/{slug}")
def get_post_by_slug(slug: str, db: Session = Depends(get_db)):
    post = db.query(Post).filter(Post.slug == slug, Post.is_removed == False).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return format_post(post)


@router.get("/{post_id}")
def get_post(post_id: str, db: Session = Depends(get_db)):
    post = db.query(Post).filter(Post.id == post_id, Post.is_removed == False).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return format_post(post)


@router.delete("/{post_id}")
def delete_post(
    post_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    post = db.query(Post).filter(Post.id == post_id).first()

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if str(post.author_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Not your post")

    post.is_removed = True
    db.commit()

    return {"message": "Post deleted"}


@router.post("/{post_id}/vote")
def vote_post(
    post_id: str,
    value: int,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    if value not in (1, -1):
        raise HTTPException(status_code=400, detail="Vote value must be 1 or -1")

    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    post = db.query(Post).filter(Post.id == post_id, Post.is_removed == False).first()

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    existing = db.query(Vote).filter(Vote.user_id == user.id, Vote.post_id == post.id).first()

    if existing:
        if existing.value == value:
            if value == 1:
                post.upvotes = max(0, post.upvotes - 1)
            else:
                post.downvotes = max(0, post.downvotes - 1)
            db.delete(existing)
            db.commit()
            return {"message": "Vote removed"}
        else:
            if value == 1:
                post.upvotes += 1
                post.downvotes = max(0, post.downvotes - 1)
            else:
                post.downvotes += 1
                post.upvotes = max(0, post.upvotes - 1)
            existing.value = value
    else:
        vote = Vote(user_id=user.id, post_id=post.id, value=value)
        db.add(vote)
        if value == 1:
            post.upvotes += 1
        else:
            post.downvotes += 1

    db.commit()
    return {"message": "Vote recorded", "upvotes": post.upvotes, "downvotes": post.downvotes}