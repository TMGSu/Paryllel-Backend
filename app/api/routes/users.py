from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.core.deps import get_db, get_db_with_clerk_id
from app.core.auth import verify_token
from app.models.community import Community
from app.models.community_member import CommunityMember
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])


class UpdateUser(BaseModel):
    username: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    banner_url: Optional[str] = None


def format_user(user: User):
    return {
        "id": str(user.id),
        "clerk_user_id": user.clerk_user_id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "bio": user.bio,
        "avatar_url": user.avatar_url,
        "banner_url": user.banner_url,
        "reputation": user.reputation,
        "total_earned": float(user.total_earned) if user.total_earned is not None else 0.0,
        "is_verified": user.is_verified,
        "is_banned": user.is_banned,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


@router.post("/me")
def create_or_get_user(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    email = payload.get("email")  # ✅ pull email from JWT

    print("📨 Payload:", payload)  # ← add this
    print("📧 Email:", email)       # ← add this

    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()

    if user:
        return {"message": "User exists", "user": format_user(user)}

    # ✅ store clerk_user_id + email on creation
    new_user = User(clerk_user_id=clerk_user_id, email=email)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return {"message": "User created", "user": format_user(new_user)}


@router.patch("/me")
def update_user(
    updates: UpdateUser,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]

    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if updates.username is not None:
        user.username = updates.username
    if updates.display_name is not None:
        user.display_name = updates.display_name
    if updates.avatar_url is not None:
        user.avatar_url = updates.avatar_url
    if updates.banner_url is not None:
        user.banner_url = updates.banner_url


    db.commit()
    db.refresh(user)

    return {"message": "User updated", "user": format_user(user)}

@router.get("/me/posts")
def get_my_posts(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    from app.models.post import Post
    from app.models.community import Community
    from app.models.community_member import CommunityMember
    from app.models.community_subscription import CommunitySubscription
    from app.models.vote import Vote
    from app.models.comment import Comment
    from sqlalchemy import func
    from datetime import datetime, timezone

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Fetch all of this user's posts with community + counts
    rows = (
        db.query(
            Post,
            Community,
            
            func.count(Comment.id).label("comments"),
        )
        .join(Community, Post.community_id == Community.id)
        .outerjoin(Comment, Comment.post_id == Post.id)
        .filter(Post.author_id == user.id, Post.is_removed == False)
        .group_by(Post.id, Community.id)
        .order_by(Post.created_at.desc())
        .all()
    )

    # Preload viewer's community memberships and active subscriptions in one query each
    member_community_ids = {
        str(m.community_id)
        for m in db.query(CommunityMember.community_id)
        .filter(CommunityMember.user_id == user.id)
        .all()
    }

    now = datetime.now(timezone.utc)
    subscribed_community_ids = {
        str(s.community_id)
        for s in db.query(CommunitySubscription.community_id)
        .filter(
            CommunitySubscription.subscriber_user_id == user.id,
            CommunitySubscription.status == "active",
            CommunitySubscription.current_period_end > now,
        )
        .all()
    }

    result = []
    for row in rows:
        post: Post = row.Post
        community: Community = row.Community
        cid = str(community.id)

        # --- Visibility checks ---

        # 1. NSFW: hide from viewers who haven't opted in
        #    (always show the post owner their own posts)
        if post.is_nsfw and not getattr(user, "nsfw_enabled", False) and post.author_id != user.id:
            continue

        if community.is_private and cid not in member_community_ids and post.author_id != user.id:
            continue

        if post.subscriber_only and cid not in subscribed_community_ids and post.author_id != user.id:
            continue

        result.append({
            "id": str(post.id),
            "title": post.title,
            "body": post.body,
            "slug": post.slug,
            "created_at": post.created_at.isoformat() if post.created_at else None,
            "is_nsfw": post.is_nsfw,
            "is_subscribers_only": post.subscriber_only,
            "community": community.name,
            "community_display_name": community.display_name,
            "community_color": getattr(community, "color", None),
            "community_icon_url": community.icon_url,
            "community_is_private": community.is_private,
            "community_is_nsfw": community.is_nsfw,
            "upvotes": post.upvotes,
            "comments": row.comments,
        })

    return result


@router.get("/me/comments")
def get_my_comments(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    from app.models.comment import Comment
    from app.models.post import Post
    from app.models.community import Community
    from sqlalchemy import func

    rows = (
        db.query(
            Comment,
            Post.title.label("post_title"),
            Post.slug.label("post_slug"),
            Community.name.label("community"),
            
            
        )
        .join(Post, Comment.post_id == Post.id)
        .join(Community, Post.community_id == Community.id)
        .filter(Comment.author_id == user.id, Comment.is_removed == False)
        .group_by(Comment.id, Post.title, Post.slug, Community.name)
        .order_by(Comment.created_at.desc())
        .all()
    )

    return [
        {
            "id": str(r.Comment.id),
            "body": r.Comment.body,
            "created_at": r.Comment.created_at.isoformat() if r.Comment.created_at else None,
            "post_title": r.post_title,
            "post_slug": r.post_slug,
            "community": r.community,
            "community_color": None,
            "upvotes": r.Comment.upvotes,
        }
        for r in rows
    ]


@router.get("/me/tips")
def get_my_tips(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    from app.models.tip import Tip
    from app.models.post import Post

    rows = (
        db.query(
            Tip,
            User.username.label("from_username"),
            Post.title.label("post_title"),
            Post.slug.label("post_slug"),
        )
        .join(User, Tip.from_user_id == User.id)
        .join(Post, Tip.post_id == Post.id)
        .filter(Tip.to_user_id == user.id, Tip.status == "completed")
        .order_by(Tip.created_at.desc())
        .all()
    )

    tips = [
        {
            "amount_cents": r.Tip.creator_amount_cents,
            "amount": round(r.Tip.creator_amount_cents / 100, 2),
            "created_at": r.Tip.created_at.isoformat() if r.Tip.created_at else None,
            "from_username": r.from_username,
            "post_title": r.post_title,
            "post_slug": r.post_slug,
        }
        for r in rows
    ]

    return {
        "tips": tips,
        "total": round(sum(t["amount"] for t in tips), 2),
    }
    
@router.get("/me/communities")
def get_my_communities(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    from app.models.community import Community
    from app.models.community_member import CommunityMember
    from app.models.post import Post
    from sqlalchemy import func

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    rows = (
        db.query(
            Community,
            CommunityMember.is_moderator,
            func.count(Post.id).label("post_count"),
        )
        .join(CommunityMember, CommunityMember.community_id == Community.id)
        .outerjoin(
            Post,
            (Post.community_id == Community.id) & (Post.author_id == user.id),
        )
        .filter(CommunityMember.user_id == user.id)
        .group_by(Community.id, CommunityMember.is_moderator)
        .order_by(func.count(Post.id).desc())
        .all()
    )

    return [
        {
            "id": str(r.Community.id),
            "name": r.Community.name,
            "display_name": r.Community.display_name,
            "icon_url": r.Community.icon_url,
            "color": getattr(r.Community, "color", None),
            "is_private": r.Community.is_private,
            "is_nsfw": r.Community.is_nsfw,
            "is_moderator": r.is_moderator,
            "post_count": r.post_count,
        }
        for r in rows
    ]
    
@router.get("/me/subscriptions")
def get_my_subscriptions(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)

    from app.models.community_subscription import CommunitySubscription
    from app.models.community import Community
    from datetime import datetime, timezone

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.now(timezone.utc)
    rows = (
        db.query(CommunitySubscription, Community)
        .join(Community, CommunitySubscription.community_id == Community.id)
        .filter(
            CommunitySubscription.subscriber_user_id == user.id,
            CommunitySubscription.status.in_(["active", "past_due"]),
        )
        .order_by(CommunitySubscription.created_at.desc())
        .all()
    )

    return [
        {
            "id": str(r.CommunitySubscription.id),
            "community_name": r.Community.name,
            "community_display_name": r.Community.display_name,
            "community_icon_url": r.Community.icon_url,
            "status": r.CommunitySubscription.status,
            "price_cents": r.Community.subscription_price_cents,
            "current_period_end": r.CommunitySubscription.current_period_end.isoformat() if r.CommunitySubscription.current_period_end else None,
            "cancel_at_period_end": r.CommunitySubscription.cancel_at_period_end,
        }
        for r in rows
    ]

@router.patch("/me/settings")
def update_user_settings(
    body: dict,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if "show_nsfw" in body:
        user.show_nsfw = bool(body["show_nsfw"])
    db.commit()
    return {"message": "Settings updated", "show_nsfw": user.show_nsfw}

@router.get("/me/communities")
def get_my_communities(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    clerk_user_id = payload["sub"]
    get_db_with_clerk_id(clerk_user_id, db)
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    rows = (
        db.query(CommunityMember, Community)
        .join(Community, Community.id == CommunityMember.community_id)
        .filter(CommunityMember.user_id == user.id)
        .order_by(CommunityMember.joined_at.desc())
        .all()
    )

    return {
        "communities": [
            {
                "name": c.name,
                "display_name": c.display_name,
                "icon_url": c.icon_url,
                "member_count": c.member_count,
                "is_moderator": cm.is_moderator,
            }
            for cm, c in rows
        ]
    }