# app/services/subscription_service.py
"""
All business logic for community subscriptions.
Stripe Connect destination charges — same pattern as tip_service.
"""
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
import stripe

from app.core.config import settings
from app.models.community import Community
from app.models.community_subscription_plan import CommunitySubscriptionPlan
from app.models.community_subscription import CommunitySubscription
from app.models.user import User

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_stripe_customer(user: User, db: Session) -> str:
    """
    One Stripe Customer per platform user — created on first subscription,
    reused for all subsequent ones across every community.
    """
    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer = stripe.Customer.create(
        email    = user.email or "",
        name     = user.display_name or user.username or "",
        metadata = {"platform_user_id": str(user.id)},
    )
    user.stripe_customer_id = customer.id
    db.flush()
    logger.info("stripe_customer_created", extra={"user_id": str(user.id), "customer_id": customer.id})
    return customer.id


def _active_subscription(community_id, subscriber_user_id, db: Session) -> CommunitySubscription | None:
    """
    Returns a subscription record that grants current access:
    status=active OR (status=canceled but current_period_end is in the future).
    """
    now = datetime.now(timezone.utc)
    # Access policy:
    # - active: full access
    # - past_due: grace period access (payment failed but retrying — intentional product choice)
    # - cancel_at_period_end=True but still active: access until period ends
    # - canceled: access only if current_period_end is in the future (already paid period)
    return (
        db.query(CommunitySubscription)
        .filter(
            CommunitySubscription.community_id       == community_id,
            CommunitySubscription.subscriber_user_id == subscriber_user_id,
            CommunitySubscription.status.in_(["active", "past_due"]),
        )
        .first()
        or
        db.query(CommunitySubscription)
        .filter(
            CommunitySubscription.community_id       == community_id,
            CommunitySubscription.subscriber_user_id == subscriber_user_id,
            CommunitySubscription.status             == "canceled",
            CommunitySubscription.current_period_end >  now,
        )
        .first()
    )


def is_subscribed(community_id, subscriber_user_id, db: Session) -> bool:
    return _active_subscription(community_id, subscriber_user_id, db) is not None

def is_subscribed_from_record(sub: CommunitySubscription | None) -> bool:
    """
    Derives subscription access from an already-fetched record.
    Avoids a second DB query in the status endpoint.
    """
    if sub is None:
        return False
    if sub.status in ("active", "past_due"):
        return True
    if sub.status == "canceled":
        period_end = sub.current_period_end
        if period_end.tzinfo is None:
            period_end = period_end.replace(tzinfo=timezone.utc)
        return period_end > datetime.now(timezone.utc)
    return False


# ---------------------------------------------------------------------------
# Setup — owner enables subscriptions / changes price
# ---------------------------------------------------------------------------

def setup_subscription_plan(
    community: Community,
    owner: User,
    price_cents: int,
    db: Session,
) -> CommunitySubscriptionPlan:
    """
    Creates or updates the Stripe Product + Price for a community.
    Validates that the owner has completed Connect onboarding.
    """
    # PLACEHOLDER assumption: owner has stripe_account_id + stripe_onboarding_complete columns.
    # Adjust attribute names if yours differ.
    if not getattr(owner, "stripe_account_id", None) or not getattr(owner, "stripe_onboarding_complete", False):
        raise ValueError("You must complete Stripe Connect onboarding before enabling subscriptions.")

    if price_cents < 100:
        raise ValueError("Minimum subscription price is $1.00.")
    if price_cents > 100_000:
        raise ValueError("Maximum subscription price is $1,000.00.")

    # Deactivate any existing plan
    existing_plan = (
        db.query(CommunitySubscriptionPlan)
        .filter(
            CommunitySubscriptionPlan.community_id == community.id,
            CommunitySubscriptionPlan.is_active    == True,
        )
        .first()
    )
    # Reuse existing product ID or create a new one
    product_id = existing_plan.stripe_product_id if existing_plan else None
    if not product_id:
        product = stripe.Product.create(
            name     = f"{community.display_name or community.name} — Monthly Subscription",
            metadata = {"community_id": str(community.id)},
        )
        product_id = product.id

    # Create new Stripe Price FIRST — before retiring the old one.
    # If this fails, old plan stays active and community is not left without a plan.
    price = stripe.Price.create(
        product        = product_id,
        unit_amount    = price_cents,
        currency       = "usd",
        recurring      = {"interval": "month"},
        metadata       = {"community_id": str(community.id)},
    )

    # New plan created in DB — only NOW retire the old one
    plan = CommunitySubscriptionPlan(
        community_id      = community.id,
        price_cents       = price_cents,
        currency          = "usd",
        stripe_product_id = product_id,
        stripe_price_id   = price.id,
        is_active         = True,
    )
    db.add(plan)

    if existing_plan:
        try:
            stripe.Price.modify(existing_plan.stripe_price_id, active=False)
        except stripe.error.StripeError:
            logger.warning("Could not deactivate old Stripe price", extra={"price_id": existing_plan.stripe_price_id})
        existing_plan.is_active = False

    community.subscription_enabled     = True
    community.subscription_price_cents = price_cents
    db.flush()

    logger.info("subscription_plan_created", extra={
        "community_id": str(community.id),
        "price_cents":  price_cents,
        "stripe_price": price.id,
    })
    return plan


# ---------------------------------------------------------------------------
# Subscribe — subscriber pays
# ---------------------------------------------------------------------------

def create_community_subscription(
    community:        Community,
    plan:             CommunitySubscriptionPlan,
    subscriber:       User,
    owner:            User,
    payment_method_id: str,
    db:               Session,
) -> CommunitySubscription:
    """
    Creates a Stripe Subscription using Connect destination charges.
    application_fee_percent is taken from the community owner's platform_fee_pct.
    """
    # Guard: already subscribed
    existing = _active_subscription(community.id, subscriber.id, db)
    if existing:
        raise ValueError("You are already subscribed to this community.")

    # Guard: can't subscribe to own community
    if community.created_by == subscriber.id:
        raise ValueError("You cannot subscribe to your own community.")

    customer_id = _get_or_create_stripe_customer(subscriber, db)

    # Attach payment method to customer
    stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)
    stripe.Customer.modify(
        customer_id,
        invoice_settings={"default_payment_method": payment_method_id},
    )

    # PLACEHOLDER assumption: owner has platform_fee_pct (int, e.g. 20 = 20%).
    # Adjust attribute name if yours differs.
    fee_pct = getattr(owner, "platform_fee_pct", settings.DEFAULT_PLATFORM_FEE_PCT)

    stripe_sub = stripe.Subscription.create(
        customer               = customer_id,
        items                  = [{"price": plan.stripe_price_id}],
        application_fee_percent= fee_pct,
        transfer_data          = {"destination": owner.stripe_account_id},
        expand                 = ["latest_invoice.payment_intent"],
        metadata               = {
            "community_id":       str(community.id),
            "subscriber_user_id": str(subscriber.id),
            "owner_user_id":      str(owner.id),
        },
    )

    raw_period_end = getattr(stripe_sub, "current_period_end", None)
    if raw_period_end:
        period_end = datetime.fromtimestamp(raw_period_end, tz=timezone.utc)
    else:
        period_end = datetime.now(timezone.utc) + timedelta(days=30)

    sub_record = CommunitySubscription(
        community_id           = community.id,
        subscriber_user_id     = subscriber.id,
        stripe_subscription_id = stripe_sub.id,
        stripe_customer_id     = customer_id,
        status                 = stripe_sub.status,
        current_period_end     = period_end,
    )
    db.add(sub_record)
    db.flush()

    logger.info("community_subscription_created", extra={
        "community_id":  str(community.id),
        "subscriber_id": str(subscriber.id),
        "stripe_sub_id": stripe_sub.id,
        "status":        stripe_sub.status,
    })
    return sub_record


# ---------------------------------------------------------------------------
# Cancel — at period end
# ---------------------------------------------------------------------------

def cancel_community_subscription(
    community:  Community,
    subscriber: User,
    db:         Session,
) -> CommunitySubscription:
    sub = (
        db.query(CommunitySubscription)
        .filter(
            CommunitySubscription.community_id       == community.id,
            CommunitySubscription.subscriber_user_id == subscriber.id,
            CommunitySubscription.status.in_(["active", "past_due"]),
        )
        .first()
    )
    if not sub:
        raise ValueError("No active subscription found.")

    stripe.Subscription.modify(
        sub.stripe_subscription_id,
        cancel_at_period_end=True,
    )

    # Do NOT set status="canceled" — subscription is still active until period end.
    # Stripe will fire customer.subscription.updated with cancel_at_period_end=true,
    # then customer.subscription.deleted when it actually expires.
    # Webhook truth drives the terminal state.
    sub.cancel_at_period_end = True
    db.flush()

    logger.info("community_subscription_cancel_scheduled", extra={
        "community_id":  str(community.id),
        "subscriber_id": str(subscriber.id),
        "access_until":  sub.current_period_end.isoformat(),
    })
    return sub


# ---------------------------------------------------------------------------
# Webhook handlers (called from webhook_service.py)
# ---------------------------------------------------------------------------

def on_subscription_updated(stripe_sub: dict, db: Session) -> None:
    """customer.subscription.updated"""
    record = (
        db.query(CommunitySubscription)
        .filter(CommunitySubscription.stripe_subscription_id == stripe_sub["id"])
        .first()
    )
    if not record:
        logger.warning("subscription_updated: no record found", extra={"stripe_sub_id": stripe_sub["id"]})
        return

    record.status = stripe_sub["status"]
    record.current_period_end = datetime.fromtimestamp(
        stripe_sub["current_period_end"], tz=timezone.utc
    )
    db.flush()
    logger.info("subscription_updated", extra={
        "stripe_sub_id": stripe_sub["id"],
        "status":        stripe_sub["status"],
    })


def on_subscription_deleted(stripe_sub: dict, db: Session) -> None:
    """customer.subscription.deleted — hard cancel, period is over"""
    record = (
        db.query(CommunitySubscription)
        .filter(CommunitySubscription.stripe_subscription_id == stripe_sub["id"])
        .first()
    )
    if not record:
        return

    record.status = "canceled"
    record.current_period_end = datetime.fromtimestamp(
        stripe_sub["current_period_end"], tz=timezone.utc
    )
    db.flush()
    logger.info("subscription_deleted", extra={"stripe_sub_id": stripe_sub["id"]})


def on_invoice_payment_failed(invoice: dict, db: Session) -> None:
    """invoice.payment_failed — mark past_due"""
    stripe_sub_id = invoice.get("subscription")
    if not stripe_sub_id:
        return

    record = (
        db.query(CommunitySubscription)
        .filter(CommunitySubscription.stripe_subscription_id == stripe_sub_id)
        .first()
    )
    if not record:
        return

    record.status = "past_due"
    db.flush()
    logger.warning("subscription_payment_failed", extra={
        "stripe_sub_id": stripe_sub_id,
        "invoice_id":    invoice.get("id"),
    })
    
def on_subscription_created(stripe_sub: dict, db: Session) -> None:
    """customer.subscription.created — treat same as updated"""
    on_subscription_updated(stripe_sub, db)


def on_invoice_payment_succeeded(invoice: dict, db: Session) -> None:
    """invoice.payment_succeeded — reactivate any non-active subscription and sync period end."""
    stripe_sub_id = invoice.get("subscription")
    if not stripe_sub_id:
        return

    record = (
        db.query(CommunitySubscription)
        .filter(CommunitySubscription.stripe_subscription_id == stripe_sub_id)
        .first()
    )
    if not record:
        return

    # Reactivate from any recoverable non-active state
    if record.status in ("past_due", "incomplete"):
        record.status = "active"

    # Sync period end from invoice if available
    # Prefer subscription-level period end from the invoice lines
    lines = invoice.get("lines", {}).get("data", [])
    period_end = None
    for line in lines:
        if line.get("type") == "subscription":
            period_end = line.get("period", {}).get("end")
            break
    if not period_end:
        period_end = invoice.get("period_end")
    if period_end:
        record.current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)

    record.cancel_at_period_end = False
    db.flush()
    logger.info("subscription_invoice_paid", extra={
        "stripe_sub_id": stripe_sub_id,
        "status":        record.status,
    })