# app/api/routes/subscriptions.py
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import stripe

from app.core.deps import get_db
from app.core.auth import verify_token
from app.models.community import Community
from app.models.community_subscription import CommunitySubscription
from app.models.community_subscription_plan import CommunitySubscriptionPlan
from app.models.user import User
from app.schemas.subscriptions import (
    SubscriptionSetupRequest,
    SubscriptionSetupResponse,
    SubscribeRequest,
    SubscribeResponse,
    CancelResponse,
    SubscriptionStatusResponse,
)
from app.services import subscription_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/communities", tags=["subscriptions"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def get_current_user(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
) -> User:
    user = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def get_community_or_404(name: str, db: Session = Depends(get_db)) -> Community:
    community = db.query(Community).filter(Community.name == name).first()
    if not community:
        raise HTTPException(status_code=404, detail="Community not found")
    return community


def get_active_plan_or_404(community: Community, db: Session) -> CommunitySubscriptionPlan:
    plan = (
        db.query(CommunitySubscriptionPlan)
        .filter(
            CommunitySubscriptionPlan.community_id == community.id,
            CommunitySubscriptionPlan.is_active.is_(True),
        )
        .first()
    )
    if not plan:
        raise HTTPException(status_code=400, detail="No active subscription plan found for this community")
    return plan


def require_community_owner(community: Community, user: User) -> None:
    if community.created_by != user.id:
        raise HTTPException(status_code=403, detail="Only the community owner can do this")


# ---------------------------------------------------------------------------
# POST /communities/{name}/subscription/setup
# ---------------------------------------------------------------------------

@router.post("/{name}/subscription/setup", response_model=SubscriptionSetupResponse)
def setup_subscription(
    name: str,
    body: SubscriptionSetupRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    community = get_community_or_404(name, db)
    require_community_owner(community, current_user)

    try:
        plan = subscription_service.setup_subscription_plan(
            community, current_user, body.price_cents, db
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except stripe.error.StripeError as e:
        db.rollback()
        logger.error("stripe_error.setup_plan", extra={
            "user_id": str(current_user.id),
            "community_id": str(community.id),
            "error": str(e),
        })
        raise HTTPException(status_code=502, detail="Stripe error during plan setup. Please try again.")
    except Exception as e:
        db.rollback()
        logger.exception("unexpected_error.setup_plan", extra={
            "user_id": str(current_user.id),
            "community_id": str(community.id),
        })
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")

    return SubscriptionSetupResponse(
        plan_id=str(plan.id),
        price_cents=plan.price_cents,
        stripe_price_id=plan.stripe_price_id,
        is_active=plan.is_active,
    )


# ---------------------------------------------------------------------------
# POST /communities/{name}/subscribe
# ---------------------------------------------------------------------------

@router.post("/{name}/subscribe", response_model=SubscribeResponse)
def subscribe(
    name: str,
    body: SubscribeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    community = get_community_or_404(name, db)

    if not community.subscription_enabled:
        raise HTTPException(status_code=400, detail="This community does not have subscriptions enabled")

    plan = get_active_plan_or_404(community, db)

    owner = db.query(User).filter(User.id == community.created_by).first()
    if not owner:
        raise HTTPException(status_code=400, detail="Community owner not found")

    try:
        sub = subscription_service.create_community_subscription(
            community=community,
            plan=plan,
            subscriber=current_user,
            owner=owner,
            payment_method_id=body.payment_method_id,
            db=db,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except stripe.error.CardError as e:
        db.rollback()
        logger.warning("stripe_error.card_declined", extra={
            "user_id": str(current_user.id),
            "community_id": str(community.id),
            "stripe_code": e.code,
        })
        raise HTTPException(status_code=402, detail=e.user_message or "Your card was declined.")
    except stripe.error.InvalidRequestError as e:
        db.rollback()
        logger.error("stripe_error.invalid_request", extra={
            "user_id": str(current_user.id),
            "community_id": str(community.id),
            "error": str(e),
        })
        raise HTTPException(status_code=400, detail="Invalid payment details.")
    except stripe.error.StripeError as e:
        db.rollback()
        logger.error("stripe_error.subscribe", extra={
            "user_id": str(current_user.id),
            "community_id": str(community.id),
            "error": str(e),
        })
        raise HTTPException(status_code=502, detail="Payment failed. Please try again.")
    except Exception:
        db.rollback()
        logger.exception("unexpected_error.subscribe", extra={
            "user_id": str(current_user.id),
            "community_id": str(community.id),
        })
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")

    return SubscribeResponse(
        subscription_id=str(sub.id),
        status=sub.status,
        current_period_end=sub.current_period_end,
    )


# ---------------------------------------------------------------------------
# DELETE /communities/{name}/subscribe
# ---------------------------------------------------------------------------

@router.delete("/{name}/subscribe", response_model=CancelResponse)
def cancel_subscription(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    community = get_community_or_404(name, db)

    try:
        sub = subscription_service.cancel_community_subscription(community, current_user, db)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except stripe.error.StripeError as e:
        db.rollback()
        logger.error("stripe_error.cancel", extra={
            "user_id": str(current_user.id),
            "community_id": str(community.id),
            "error": str(e),
        })
        raise HTTPException(status_code=502, detail="Failed to cancel with Stripe. Please try again.")
    except Exception:
        db.rollback()
        logger.exception("unexpected_error.cancel", extra={
            "user_id": str(current_user.id),
            "community_id": str(community.id),
        })
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")

    return CancelResponse(
        subscription_id=str(sub.id),
        status=sub.status,
        current_period_end=sub.current_period_end,
        message=f"Subscription canceled. Access continues until {sub.current_period_end.strftime('%B %d, %Y')}.",
    )


# ---------------------------------------------------------------------------
# GET /communities/{name}/subscription/status
# ---------------------------------------------------------------------------

@router.get("/{name}/subscription/status", response_model=SubscriptionStatusResponse)
def subscription_status(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    community = get_community_or_404(name, db)

    sub = (
        db.query(CommunitySubscription)
        .filter(
            CommunitySubscription.community_id == community.id,
            CommunitySubscription.subscriber_user_id == current_user.id,
        )
        .order_by(CommunitySubscription.created_at.desc())
        .first()
    )

    # Derive is_subscribed from the record we already have — no second query
    is_sub = subscription_service.is_subscribed_from_record(sub) if sub else False

    return SubscriptionStatusResponse(
        subscriptions_enabled=community.subscription_enabled,
        price_cents=community.subscription_price_cents,
        is_subscribed=is_sub,
        status=sub.status if sub else None,
        current_period_end=sub.current_period_end if sub else None,
    )