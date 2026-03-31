# app/services/tip_service.py
import stripe
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from app.core.config import settings
from app.services.fee_service import FeeBreakdown
from app.models.tip import Tip
from app.models.user import User
from app.models.post import Post

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


def get_creator_fee_pct(creator: User) -> int:
    """
    Returns the platform fee % for this creator.
    Per-creator override supports future subscription tiers (e.g. 10% for Pro).
    Falls back to platform default.
    """
    return getattr(creator, "platform_fee_pct", None) or settings.DEFAULT_PLATFORM_FEE_PCT


def validate_tip(post: Post | None, tipper: User, chosen_cents: int) -> None:
    if not post:
        raise ValueError("Post not found")
    if str(post.author_id) == str(tipper.id):
        raise ValueError("Cannot tip your own post")
    if chosen_cents < 300:
        raise ValueError("Minimum tip is $3.00")
    if chosen_cents > 100_000:
        raise ValueError("Maximum tip is $1,000")


def check_idempotency(idempotency_key: str, db: Session) -> Tip | None:
    """Returns existing tip if this idempotency key was already used."""
    return db.query(Tip).filter(Tip.idempotency_key == idempotency_key).first()


def create_payment_intent(
    fees: FeeBreakdown,
    payment_method_id: str,
    creator_stripe_id: str,
    idempotency_key: str,
    metadata: dict,
) -> stripe.PaymentIntent:
    """
    Separate charges and transfers pattern.

    Full gross_cents lands in the platform Stripe account.
    No transfer_data — funds are held here until the creator requests withdrawal.
    At withdrawal time, payout_service fires stripe.Transfer.create() to move
    creator_amount_cents to their Express account, minus the platform fee.

    creator_stripe_id is kept in metadata so the transfer destination is
    recorded at charge time and available to the payout flow.
    """
    return stripe.PaymentIntent.create(
        amount                    = fees.gross_cents,
        currency                  = "usd",
        payment_method            = payment_method_id,
        confirm                   = True,
        automatic_payment_methods = {"enabled": True, "allow_redirects": "never"},
        metadata                  = {
            **metadata,
            "creator_stripe_id": creator_stripe_id,   # stored for transfer at payout time
        },
        idempotency_key           = f"pi_{idempotency_key}",
    )


def create_tip_record(
    db: Session,
    fees: FeeBreakdown,
    tipper_id,
    author_id,
    post_id,
    payment_intent_id: str,
    idempotency_key: str,
) -> Tip:
    """
    Creates a Tip record with status='created'.
    Webhook moves it to 'completed' — we never trust the PI response alone.
    available_at is set PAYOUT_HOLD_DAYS out — funds are locked until matured.
    """
    tip = Tip(
        from_user_id             = tipper_id,
        to_user_id               = author_id,
        post_id                  = post_id,
        chosen_amount_cents      = fees.chosen_cents,
        gross_amount_cents       = fees.gross_cents,
        stripe_fee_cents         = fees.stripe_fee_cents,
        platform_fee_cents       = fees.platform_target_cents,
        creator_amount_cents     = fees.creator_cents,
        platform_fee_pct         = fees.platform_fee_pct,
        stripe_payment_intent_id = payment_intent_id,
        idempotency_key          = idempotency_key,
        status                   = "created",
        available_at             = datetime.now(timezone.utc) + timedelta(days=settings.PAYOUT_HOLD_DAYS),
    )
    db.add(tip)
    db.flush()
    return tip