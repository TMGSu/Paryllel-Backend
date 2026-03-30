# app/services/payout_service.py
import stripe
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.core.config import settings
from app.models.tip import Tip
from app.models.balance_entry import BalanceEntry
from app.models.payout import Payout
from app.models.user import User

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


def get_available_balance_cents(user_id, db: Session) -> int:
    """
    Available balance = sum of all ledger entries (credits and debits)
                        minus tips that are completed but not yet matured.

    Ledger entries include:
      + tip_received credits (added in payment_succeeded webhook)
      - payout_requested debits (added at payout request time)
      + payout_reversed credits (added if payout fails)
      +/- any manual adjustments

    Pending tips are credited to the ledger immediately on payment_succeeded
    but must be excluded from available balance until available_at passes.
    Disputed tips are excluded regardless of maturity.

    This function is safe to call standalone for display purposes.
    For payout eligibility, always call AFTER acquiring a row lock on the user
    to prevent concurrent requests from both reading the same pre-debit balance.
    """
    now = datetime.now(timezone.utc)

    ledger_total = db.query(func.sum(BalanceEntry.amount_cents)).filter(
        BalanceEntry.user_id == user_id
    ).scalar() or 0

    # Tips credited to ledger but not yet matured — subtract them back out
    pending = db.query(func.sum(Tip.creator_amount_cents)).filter(
        Tip.to_user_id   == user_id,
        Tip.status       == "completed",    # only count confirmed payments
        Tip.available_at >  now,            # not yet matured
        Tip.is_disputed  == False,          # never count disputed tips
    ).scalar() or 0

    return max(0, ledger_total - pending)


def get_pending_balance_cents(user_id, db: Session) -> int:
    """
    Pending = completed, non-disputed tips still within the hold period.
    These are credited to the ledger but not yet withdrawable.
    """
    now = datetime.now(timezone.utc)
    return db.query(func.sum(Tip.creator_amount_cents)).filter(
        Tip.to_user_id   == user_id,
        Tip.status       == "completed",
        Tip.available_at >  now,
        Tip.is_disputed  == False,
    ).scalar() or 0


def request_payout(user: User, amount_cents: int, db: Session) -> Payout:
    """
    Validates eligibility and fires a Stripe payout to the creator's Express account.

    RACE CONDITION PROTECTION — why this ordering matters:
    -------------------------------------------------------
    Without a row lock, two concurrent requests can both:
      1. read available balance = $50
      2. both pass the amount_cents <= available check
      3. both debit the ledger
      4. both fire stripe.Payout.create()
    Result: double payout, balance goes negative.

    Fix — acquire WITH FOR UPDATE on the user row FIRST:
      1. Request A locks user row
      2. Request B blocks at the lock
      3. Request A debits ledger, commits, releases lock
      4. Request B acquires lock, recomputes balance (now lower), may fail threshold
    Result: only one payout fires.

    The debit is written to the ledger and flushed BEFORE the Stripe API call.
    If the Stripe call fails, we immediately write a reversal entry and commit.
    The payout.failed webhook also writes a reversal as a safety net — the
    reversal entry is idempotent because it references payout.id, so double-
    reversals would need to be guarded at the caller level if ever a concern.
    """
    # ── Step 1: Acquire row lock BEFORE any reads or writes ──────────────────
    # This serialises concurrent payout requests for the same user.
    # with_for_update() issues SELECT ... FOR UPDATE in PostgreSQL.
    locked_user = (
        db.query(User)
        .filter(User.id == user.id)
        .with_for_update()
        .one()
    )

    # ── Step 2: All eligibility checks use the locked user row ───────────────
    if locked_user.payout_frozen:
        raise ValueError(f"Payouts are frozen: {locked_user.payout_frozen_reason}")

    if not locked_user.stripe_onboarding_complete or not locked_user.stripe_account_id:
        raise ValueError("Stripe account not connected")

    if amount_cents < settings.PAYOUT_THRESHOLD_CENTS:
        raise ValueError(f"Minimum payout is ${settings.PAYOUT_THRESHOLD_CENTS // 100}.00")

    # ── Step 3: Recompute balance AFTER acquiring lock ────────────────────────
    # Any concurrent request that committed a debit before us will now be
    # visible here because we're in the same transaction after the lock.
    available = get_available_balance_cents(locked_user.id, db)

    if amount_cents > available:
        raise ValueError(
            f"Insufficient available balance — "
            f"requested {amount_cents} cents, available {available} cents"
        )

    # ── Step 4: Create payout record to get an ID for the ledger entry ────────
    payout = Payout(
        user_id      = locked_user.id,
        amount_cents = amount_cents,
        status       = "requested",
    )
    db.add(payout)
    db.flush()  # generates payout.id without committing

    # ── Step 5: Debit ledger BEFORE Stripe call ───────────────────────────────
    # Flushing writes the debit to the DB within this transaction.
    # Any other request that now tries to acquire the lock will wait,
    # then see the reduced balance after we commit.
    debit = BalanceEntry(
        user_id      = locked_user.id,
        amount_cents = -amount_cents,
        entry_type   = "payout_requested",
        reference_id = payout.id,
        note         = f"Payout of {amount_cents} cents",
    )
    db.add(debit)
    db.flush()

    # ── Step 6: Fire Stripe payout ────────────────────────────────────────────
    try:
        stripe_payout = stripe.Payout.create(
            amount         = amount_cents,
            currency       = "usd",
            stripe_account = locked_user.stripe_account_id,
            metadata       = {
                "paryllel_user_id": str(locked_user.id),
                "payout_id":        str(payout.id),
            },
        )
    except stripe.error.StripeError as e:
        # Reverse the debit immediately — do not leave balance stuck
        db.add(BalanceEntry(
            user_id      = locked_user.id,
            amount_cents = amount_cents,   # positive = re-credit
            entry_type   = "payout_reversed",
            reference_id = payout.id,
            note         = f"Stripe call failed: {str(e)}",
        ))
        payout.status         = "failed"
        payout.failure_reason = str(e)
        db.commit()  # commit reversal so balance is restored even if caller swallows error

        logger.error("payout_stripe_call_failed", extra={
            "user_id":      str(locked_user.id),
            "amount_cents": amount_cents,
            "error":        str(e),
        })
        raise ValueError(f"Stripe payout failed: {str(e)}")

    # ── Step 7: Store Stripe payout ID and commit everything atomically ───────
    payout.stripe_payout_id = stripe_payout.id
    db.commit()

    logger.info("payout_requested", extra={
        "user_id":          str(locked_user.id),
        "amount_cents":     amount_cents,
        "stripe_payout_id": stripe_payout.id,
        "payout_id":        str(payout.id),
    })
    return payout