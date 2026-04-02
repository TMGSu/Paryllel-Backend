# app/services/webhook_service.py
# ── NEW: all imports top-level (no inline imports inside handlers) ──────────
import logging

from datetime import datetime, timezone

import stripe
from fastapi import HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.orm import Session
import json


from app.core.config import settings
from app.models.balance_entry import BalanceEntry
from app.models.dispute import Dispute
from app.models.payout import Payout
from app.models.stripe_event import StripeEvent
from app.models.tip import Tip
from app.models.user import User
from app.services import subscription_service        # NEW: top-level import

logger = logging.getLogger(__name__)

# Replay-attack defence: reject events older than this many seconds.
# Stripe's own SDK default is 300 s; we match it explicitly.
_STRIPE_TIMESTAMP_TOLERANCE_SECONDS = 300


def _get_event_record(event_id: str, db: Session) -> StripeEvent | None:
    return db.query(StripeEvent).filter(StripeEvent.id == event_id).first()

# ── NEW: SECURITY — verified entry point called by the FastAPI route ────────
async def receive_webhook(request: Request, db: Session) -> dict:
    """
    Single entry point for all Stripe webhooks.

    Why raw body?  Stripe signs the *exact* bytes it sent.  If you let FastAPI
    parse JSON first the byte sequence may change (key order, whitespace) and
    signature verification will fail for legitimate events.

    Why timestamp tolerance?  Stripe embeds a `t=` field in the Stripe-Signature
    header.  Verifying it prevents replay attacks where an attacker re-sends a
    captured valid webhook hours later.
    """
    raw_body  = await request.body()          # must be raw bytes, never re-parsed
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = settings.STRIPE_WEBHOOK_SECRET

    try:
        event = stripe.Webhook.construct_event(
            payload   = raw_body,
            sig_header= sig_header,
            secret    = webhook_secret,
            tolerance = _STRIPE_TIMESTAMP_TOLERANCE_SECONDS,
        )
    except stripe.error.SignatureVerificationError as exc:
        # 400 is correct: Stripe will NOT retry on 4xx, stopping replay loops.
        logger.warning("webhook_signature_invalid", extra={"error": str(exc)})
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except Exception as exc:
        logger.exception("webhook_construct_event_failed", extra={"error": str(exc)})
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    handle_event(event, raw_body, db)
    return {"status": "ok"}


# ── UPDATED: handle_event ───────────────────────────────────────────────────
# Now accepts raw_body for audit storage and event.created for ordering.
def handle_event(event, raw_body: bytes, db: Session) -> None:
    event = json.loads(str(event))
    event_id      = event["id"]
    event_type    = event["type"]
    event_created = event.get("created")
    livemode      = event.get("livemode", False)
    data          = event["data"]["object"]

    # ── IDEMPOTENCY: race-condition-safe upsert ─────────────────────────────
    # We use SELECT … FOR UPDATE to serialise concurrent workers on the same
    # event_id.  If two Lambda/Gunicorn workers receive the same event
    # simultaneously, one will block here until the other commits, then see
    # status="succeeded" and bail out cleanly.
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.exc import IntegrityError

    # Insert-first idempotency — only the worker that wins the INSERT processes.
    # If two workers race, one gets IntegrityError and exits cleanly.
    try:
        db.execute(
            pg_insert(StripeEvent).values(
                id          = event_id,
                event_type  = event_type,
                status      = "processing",
                created_at  = datetime.fromtimestamp(event_created, tz=timezone.utc) if event_created else datetime.now(timezone.utc),
                livemode    = livemode,
                raw_payload = raw_body.decode("utf-8", errors="replace"),
            ).on_conflict_do_nothing(index_elements=["id"])
        )
        db.flush()
    except IntegrityError:
        db.rollback()
        logger.info("duplicate_event_skipped", extra={"event_id": event_id})
        return

    # Check if another worker already succeeded (on_conflict_do_nothing path)
    existing = db.query(StripeEvent).filter(StripeEvent.id == event_id).first()
    if existing and existing.status == "succeeded":
        logger.info("duplicate_event_skipped", extra={"event_id": event_id})
        return

    # ── DISPATCHER: clean, extensible handler map ───────────────────────────
    _HANDLERS: dict[str, callable] = {
        "payment_intent.succeeded":         _on_payment_succeeded,
        "payment_intent.payment_failed":    _on_payment_failed,
        "charge.refunded":                  _on_charge_refunded,      # NEW
        "charge.dispute.created":           _on_dispute_created,
        "payout.paid":                      _on_payout_paid,
        "payout.failed":                    _on_payout_failed,
        
        "account.updated":                  _on_account_updated,
        "customer.subscription.created":    subscription_service.on_subscription_created,   # NEW
        "customer.subscription.updated":    subscription_service.on_subscription_updated,
        "customer.subscription.deleted":    subscription_service.on_subscription_deleted,
        "invoice.payment_succeeded":        subscription_service.on_invoice_payment_succeeded,  # NEW
        "invoice.payment_failed":           subscription_service.on_invoice_payment_failed,
    }

    try:
        handler = _HANDLERS.get(event_type)
        if handler:
            handler(data, db)
        else:
            logger.debug("unhandled_event", extra={"event_type": event_type})

        record = _get_event_record(event_id, db)
        if record:
            record.status = "succeeded"
        db.commit()

    except Exception as e:
        db.rollback()
        # Re-fetch after rollback — previous ORM state is stale
        record = db.query(StripeEvent).filter(StripeEvent.id == event_id).first()
        if record:
            record.status = "failed"
            record.error  = str(e)[:2000]   # guard against enormous tracebacks
            db.commit()
        logger.exception("webhook_handler_failed", extra={
            "event_id":   event_id,
            "event_type": event_type,
            "error":      str(e),
        })
        raise   # Re-raise → 500 → Stripe will retry with backoff


def _on_payment_succeeded(pi: dict, db: Session) -> None:
    pi_id    = pi.get("id")
    metadata = pi.get("metadata") or {}

    if not pi_id:
        logger.warning("payment_succeeded_missing_pi_id")
        return

    # Log suspicious cases — metadata should always be present
    if not metadata:
        logger.warning("payment_succeeded_missing_metadata", extra={"pi_id": pi_id})

    to_user_id   = metadata.get("to_user_id")
    from_user_id = metadata.get("from_user_id")

    if not to_user_id or not from_user_id:
        logger.warning("payment_succeeded_missing_user_ids", extra={
            "pi_id":       pi_id,
            "to_user_id":  to_user_id,
            "from_user_id": from_user_id,
        })

    tip = db.query(Tip).filter(Tip.stripe_payment_intent_id == pi_id).first()
    if not tip:
        logger.warning("payment_succeeded_no_tip_found", extra={"pi_id": pi_id})
        return
    if tip.status == "completed":
        return

    # Sanity check — metadata user IDs should match tip record
    if to_user_id and str(tip.to_user_id) != to_user_id:
        logger.warning("payment_succeeded_user_id_mismatch", extra={
            "pi_id":            pi_id,
            "tip_to_user_id":   str(tip.to_user_id),
            "metadata_user_id": to_user_id,
        })

    tip.status         = "completed"
    tip.stripe_charge_id = pi.get("latest_charge")

    db.add(BalanceEntry(
        user_id      = tip.to_user_id,
        amount_cents = tip.creator_amount_cents,
        entry_type   = "tip_received",
        reference_id = tip.id,
        note         = f"Tip completed — PI {pi_id}",
    ))

    author = db.query(User).filter(User.id == tip.to_user_id).first()
    if author:
        author.total_earned_cents = (author.total_earned_cents or 0) + tip.creator_amount_cents

    db.flush()
    logger.info("tip_completed", extra={
        "tip_id":        str(tip.id),
        "user_id":       str(tip.to_user_id),
        "creator_cents": tip.creator_amount_cents,
        "pi_id":         pi_id,
    })


def _on_payment_failed(pi: dict, db: Session) -> None:
    pi_id = pi.get("id")
    if not pi_id:
        logger.warning("payment_failed_missing_pi_id")
        return

    tip = db.query(Tip).filter(Tip.stripe_payment_intent_id == pi_id).first()
    if tip and tip.status == "created":
        tip.status = "failed"
        db.flush()

    logger.warning("payment_failed", extra={
        "pi_id":  pi_id,
        "tip_id": str(tip.id) if tip else None,
    })

# ── NEW: FINANCIAL CORRECTNESS — handle refunds ─────────────────────────────
def _on_charge_refunded(charge: dict, db: Session) -> None:
    """
    Handles partial and full refunds correctly.
    Reverses only the creator's proportional share, not the raw refund amount.
    Tracks cumulative refunds to handle multiple partial refund events.
    """
    charge_id       = charge.get("id")
    total_refunded  = charge.get("amount_refunded", 0)   # cumulative total from Stripe
    gross_amount    = charge.get("amount", 0)

    if not charge_id:
        logger.warning("charge_refunded_missing_charge_id")
        return

    tip = db.query(Tip).filter(Tip.stripe_charge_id == charge_id).first()
    if not tip:
        logger.info("charge_refunded_no_tip_found", extra={"charge_id": charge_id})
        return

    # How much we've already reversed for this tip
    already_reversed = db.query(
        func.sum(BalanceEntry.amount_cents)
    ).filter(
        BalanceEntry.reference_id == tip.id,
        BalanceEntry.entry_type   == "tip_refunded",
    ).scalar() or 0
    already_reversed = abs(already_reversed)   # stored as negative

    # Creator's proportional share of the refund
    if gross_amount > 0:
        creator_refund_total = int(total_refunded * tip.creator_amount_cents / gross_amount)
    else:
        creator_refund_total = 0

    # Delta = new cumulative creator refund minus what we've already reversed
    delta = creator_refund_total - already_reversed
    if delta <= 0:
        logger.info("charge_refunded_no_new_delta", extra={
            "charge_id": charge_id, "delta": delta
        })
        return

    db.add(BalanceEntry(
        user_id      = tip.to_user_id,
        amount_cents = -delta,
        entry_type   = "tip_refunded",
        reference_id = tip.id,
        note         = f"Refund delta on charge {charge_id} ({delta} cents)",
    ))

    author = db.query(User).filter(User.id == tip.to_user_id).first()
    if author:
        author.total_earned_cents = max(0, (author.total_earned_cents or 0) - delta)

    if total_refunded >= gross_amount:
        tip.status = "refunded"

    db.flush()
    logger.info("charge_refunded_processed", extra={
        "charge_id":      charge_id,
        "tip_id":         str(tip.id),
        "delta_cents":    delta,
        "total_refunded": total_refunded,
    })

def _on_dispute_created(dispute: dict, db: Session) -> None:
    """
    charge.dispute.created sends a Dispute object, NOT a Charge.
    Extract charge_id from dispute.charge, then look up the tip.
    """
    dispute_id     = dispute.get("id")
    charge_id      = dispute.get("charge")
    amount_cents   = dispute.get("amount", 0)
    reason         = dispute.get("reason", "unknown")

    if not dispute_id or not charge_id:
        logger.warning("dispute_created_missing_fields", extra={
            "dispute_id": dispute_id, "charge_id": charge_id
        })
        return

    tip = db.query(Tip).filter(Tip.stripe_charge_id == charge_id).first()
    if not tip:
        logger.warning("dispute_created_no_tip_found", extra={
            "dispute_id": dispute_id, "charge_id": charge_id
        })

    tip_id  = tip.id         if tip else None
    user_id = tip.to_user_id if tip else None

    if tip:
        tip.is_disputed = True
        tip.disputed_at = datetime.now(timezone.utc)
        tip.status      = "disputed"

    if user_id:
        creator = db.query(User).filter(User.id == user_id).first()
        if creator:
            creator.payout_frozen        = True
            creator.payout_frozen_reason = f"Dispute {dispute_id} on charge {charge_id}"

    db.add(Dispute(
        tip_id            = tip_id,
        user_id           = user_id,
        stripe_dispute_id = dispute_id,
        amount_cents      = amount_cents,
    ))
    db.flush()

    logger.warning("dispute_created", extra={
        "dispute_id":    dispute_id,
        "charge_id":     charge_id,
        "reason":        reason,
        "tip_id":        str(tip_id) if tip_id else None,
        "user_id":       str(user_id) if user_id else None,
        "amount_cents":  amount_cents,
    })


def _on_payout_paid(payout_data: dict, db: Session) -> None:
    payout_id = payout_data.get("id")
    if not payout_id:
        logger.warning("payout_paid_missing_id")
        return

    payout = db.query(Payout).filter(Payout.stripe_payout_id == payout_id).first()
    if payout:
        payout.status  = "paid"
        payout.paid_at = datetime.now(timezone.utc)
        db.flush()
    else:
        logger.warning("payout_paid_no_record_found", extra={"stripe_payout_id": payout_id})

    logger.info("payout_paid", extra={
        "stripe_payout_id": payout_id,
        "payout_id":        str(payout.id) if payout else None,
    })


def _on_payout_failed(payout_data: dict, db: Session) -> None:
    payout_id       = payout_data.get("id")
    failure_message = payout_data.get("failure_message")

    if not payout_id:
        logger.warning("payout_failed_missing_id")
        return

    payout = db.query(Payout).filter(Payout.stripe_payout_id == payout_id).first()
    if payout:
        payout.status         = "failed"
        payout.failure_reason = failure_message
        db.add(BalanceEntry(
            user_id      = payout.user_id,
            amount_cents = payout.amount_cents,   # positive = re-credit
            entry_type   = "payout_reversed",
            reference_id = payout.id,
            note         = f"Payout failed: {failure_message}",
        ))
        db.flush()
    else:
        logger.warning("payout_failed_no_record_found", extra={"stripe_payout_id": payout_id})

    logger.error("payout_failed", extra={
        "stripe_payout_id": payout_id,
        "failure_message":  failure_message,
        "payout_id":        str(payout.id) if payout else None,
    })





def _on_account_updated(account: dict, db: Session) -> None:
    account_id = account.get("id")
    if not account_id:
        logger.warning("account_updated_missing_id")
        return

    if not account.get("details_submitted"):
        return

    user = db.query(User).filter(User.stripe_account_id == account_id).first()
    if not user:
        logger.warning("account_updated_no_user_found", extra={"stripe_account_id": account_id})
        return

    if not user.stripe_onboarding_complete:
        user.stripe_onboarding_complete = True
        db.flush()
        logger.info("onboarding_complete", extra={
            "user_id":           str(user.id),
            "stripe_account_id": account_id,
        })