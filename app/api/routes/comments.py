from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from typing import Optional

from app.core.deps import get_db, get_db_with_clerk_id
from app.core.auth import verify_token
from app.models.comment import Comment
from app.models.post import Post
from app.models.user import User
from app.models.vote import Vote

router = APIRouter(prefix="/comments", tags=["comments"])


class CreateComment(BaseModel):
    post_id: str
    body: str
    parent_id: Optional[str] = None


def format_comment(c: Comment):
    return {
        "id": str(c.id),
        "post_id": str(c.post_id),
        "parent_id": str(c.parent_id) if c.parent_id else None,
        "body": c.body,
        "upvotes": c.upvotes,
        "downvotes": c.downvotes,
        "is_removed": c.is_removed,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "author": {
            "username": c.author.username,
            "display_name": c.author.display_name,
            "avatar_url": c.author.avatar_url,
            "reputation": c.author.reputation,
        } if c.author else None,
    }


@router.get("/")
def list_comments(post_id: str, db: Session = Depends(get_db)):
    comments = db.query(Comment).filter(
        Comment.post_id == post_id,
        Comment.is_removed == False,
        Comment.parent_id == None
    ).order_by(desc(Comment.upvotes)).all()
    return {"comments": [format_comment(c) for c in comments]}


@router.post("/")
def create_comment(
    body: CreateComment,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    post = db.query(Post).filter(Post.id == body.post_id, Post.is_removed == False).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if post.is_locked:
        raise HTTPException(status_code=400, detail="Post is locked")

    if not body.body.strip():
        raise HTTPException(status_code=400, detail="Comment body is required")

    comment = Comment(
        post_id=body.post_id,
        author_id=user.id,
        body=body.body.strip(),
        parent_id=body.parent_id,
    )
    db.add(comment)
    post.comment_count += 1

    # Bump reputation
    user.reputation += 2
    db.commit()
    db.refresh(comment)

    from app.services.notification_service import create_notification

    # Notify post author of new comment (not if they comment on their own post)
    if str(user.id) != str(post.author_id):
        create_notification(
            db,
            user_id=str(post.author_id),
            type="comment",
            title=f"u/{user.username} commented on your post",
            body=f"\"{post.title}\"",
            link=f"/posts/{post.slug}",
        )

    # Notify parent comment author of reply (not if replying to own comment or post author already notified)
    if body.parent_id:
        parent = db.query(Comment).filter(Comment.id == body.parent_id).first()
        if parent and str(parent.author_id) != str(user.id) and str(parent.author_id) != str(post.author_id):
            create_notification(
                db,
                user_id=str(parent.author_id),
                type="reply",
                title=f"u/{user.username} replied to your comment",
                body=f"on \"{post.title}\"",
                link=f"/posts/{post.slug}",
            )

    db.commit()

    return {"message": "Comment created", "comment": format_comment(comment)}


@router.delete("/{comment_id}")
def delete_comment(
    comment_id: str,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    comment = db.query(Comment).filter(Comment.id == comment_id).first()

    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if str(comment.author_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Not your comment")

    comment.is_removed = True
    db.commit()

    return {"message": "Comment deleted"}


@router.post("/{comment_id}/vote")
def vote_comment(
    comment_id: str,
    value: int,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    if value not in (1, -1):
        raise HTTPException(status_code=400, detail="Vote value must be 1 or -1")

    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    comment = db.query(Comment).filter(Comment.id == comment_id).first()

    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    existing = db.query(Vote).filter(Vote.user_id == user.id, Vote.comment_id == comment.id).first()

    if existing:
        if existing.value == value:
            if value == 1:
                comment.upvotes = max(0, comment.upvotes - 1)
            else:
                comment.downvotes = max(0, comment.downvotes - 1)
            db.delete(existing)
            db.commit()
            return {"message": "Vote removed"}
        else:
            if value == 1:
                comment.upvotes += 1
                comment.downvotes = max(0, comment.downvotes - 1)
            else:
                comment.downvotes += 1
                comment.upvotes = max(0, comment.upvotes - 1)
            existing.value = value
    else:
        vote = Vote(user_id=user.id, comment_id=comment.id, value=value)
        db.add(vote)
        if value == 1:
            comment.upvotes += 1
        else:
            comment.downvotes += 1

    db.commit()
    return {"message": "Vote recorded"}