# app/services/risk_service.py
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models.tip import Tip
from app.models.dispute import Dispute
from app.models.user import User

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

# Max tips a single user can send within the burst window
BURST_MAX_TIPS        = 5
BURST_WINDOW_MINUTES  = 10

# If a single tip exceeds this and the recipient account is < 7 days old, flag it
LARGE_TIP_THRESHOLD_CENTS = 5000   # $50

# How many days old an account must be to receive large tips without review
NEW_ACCOUNT_DAYS = 7

# How many disputes before payouts are auto-frozen
DISPUTE_FREEZE_THRESHOLD = 2
DISPUTE_LOOKBACK_DAYS    = 30


# ── Rate limiting ─────────────────────────────────────────────────────────────

def check_tip_rate_limit(tipper_id, db: Session) -> None:
    """
    Raises ValueError if the tipper has sent too many tips in the burst window.
    Call this before creating a PaymentIntent.

    5 tips in 10 minutes is the default threshold.
    Adjust BURST_MAX_TIPS and BURST_WINDOW_MINUTES in constants above.
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=BURST_WINDOW_MINUTES)

    recent_count = db.query(func.count(Tip.id)).filter(
        Tip.from_user_id == tipper_id,
        Tip.created_at   >= window_start,
        Tip.status.in_(["created", "completed"]),
    ).scalar() or 0

    if recent_count >= BURST_MAX_TIPS:
        logger.warning("tip_rate_limit_exceeded", extra={
            "tipper_id":    str(tipper_id),
            "recent_count": recent_count,
            "window_min":   BURST_WINDOW_MINUTES,
        })
        raise ValueError(
            f"Too many tips sent in a short period. Please wait before tipping again."
        )


# ── New account large tip flag ─────────────────────────────────────────────────

def check_new_account_large_tip(creator: User, chosen_cents: int, db: Session) -> None:
    """
    Logs a warning if a large tip is being sent to a very new account.
    Does NOT block the tip — just flags it for review.

    If you want to block instead, raise ValueError here.
    """
    if chosen_cents < LARGE_TIP_THRESHOLD_CENTS:
        return

    account_age = datetime.now(timezone.utc) - creator.created_at.replace(tzinfo=timezone.utc)
    if account_age.days < NEW_ACCOUNT_DAYS:
        logger.warning("large_tip_to_new_account", extra={
            "creator_id":    str(creator.id),
            "chosen_cents":  chosen_cents,
            "account_age_d": account_age.days,
        })


# ── Dispute-based auto-freeze ──────────────────────────────────────────────────

def check_and_freeze_on_disputes(user_id, db: Session) -> None:
    """
    Checks how many disputes a creator has had in the lookback window.
    If they exceed the threshold, automatically freezes their payouts.

    Called from webhook_service._on_dispute_created() after recording a dispute.
    """
    window_start = datetime.now(timezone.utc) - timedelta(days=DISPUTE_LOOKBACK_DAYS)

    dispute_count = db.query(func.count(Dispute.id)).filter(
        Dispute.user_id    == user_id,
        Dispute.created_at >= window_start,
    ).scalar() or 0

    if dispute_count >= DISPUTE_FREEZE_THRESHOLD:
        user = db.query(User).filter(User.id == user_id).first()
        if user and not user.payout_frozen:
            user.payout_frozen        = True
            user.payout_frozen_reason = (
                f"Auto-frozen: {dispute_count} disputes in the last {DISPUTE_LOOKBACK_DAYS} days"
            )
            db.flush()
            logger.warning("payout_auto_frozen_disputes", extra={
                "user_id":       str(user_id),
                "dispute_count": dispute_count,
                "lookback_days": DISPUTE_LOOKBACK_DAYS,
            })


# ── Manual freeze / unfreeze ───────────────────────────────────────────────────

def freeze_payouts(user_id, reason: str, db: Session) -> None:
    """
    Manually freeze a user's payouts. Call from admin routes or risk triggers.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.warning("freeze_payouts_user_not_found", extra={"user_id": str(user_id)})
        return

    user.payout_frozen        = True
    user.payout_frozen_reason = reason
    db.flush()

    logger.info("payout_frozen_manual", extra={
        "user_id": str(user_id),
        "reason":  reason,
    })


def unfreeze_payouts(user_id, db: Session) -> None:
    """
    Manually unfreeze a user's payouts. Call from admin routes after review.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.warning("unfreeze_payouts_user_not_found", extra={"user_id": str(user_id)})
        return

    user.payout_frozen        = False
    user.payout_frozen_reason = None
    db.flush()

    logger.info("payout_unfrozen_manual", extra={"user_id": str(user_id)})


# ── Composite pre-tip check ────────────────────────────────────────────────────

def run_pre_tip_checks(tipper_id, creator: User, chosen_cents: int, db: Session) -> None:
    """
    Single call that runs all risk checks before a tip is processed.
    Call this from tip_service or the tips route before creating a PaymentIntent.

    Raises ValueError if any hard block is triggered.
    Logs warnings for soft flags without blocking.
    """
    check_tip_rate_limit(tipper_id, db)
    check_new_account_large_tip(creator, chosen_cents, db)