# app/services/webhook_service.py
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models.tip import Tip
from app.models.user import User
from app.models.payout import Payout
from app.models.dispute import Dispute
from app.models.stripe_event import StripeEvent
from app.models.balance_entry import BalanceEntry

logger = logging.getLogger(__name__)


def _get_event_record(event_id: str, db: Session) -> StripeEvent | None:
    return db.query(StripeEvent).filter(StripeEvent.id == event_id).first()


def handle_event(event: dict, db: Session) -> None:
    event_id   = event["id"]
    event_type = event["type"]
    data       = event["data"]["object"]

    existing = _get_event_record(event_id, db)

    # Skip ONLY already-succeeded events — retry failed ones
    if existing and existing.status == "succeeded":
        logger.info("duplicate_event_skipped", extra={"event_id": event_id, "event_type": event_type})
        return

    if not existing:
        db.add(StripeEvent(id=event_id, event_type=event_type, status="processing"))
        db.flush()
    else:
        existing.status = "processing"
        db.flush()

    try:
        if event_type == "payment_intent.succeeded":
            _on_payment_succeeded(data, db)
        elif event_type == "payment_intent.payment_failed":
            _on_payment_failed(data, db)
        elif event_type == "charge.dispute.created":
            _on_dispute_created(data, db)
        elif event_type == "payout.paid":
            _on_payout_paid(data, db)
        elif event_type == "payout.failed":
            _on_payout_failed(data, db)
        elif event_type == "account.updated":
            _on_account_updated(data, db)
        else:
            logger.debug("unhandled_event", extra={"event_type": event_type})

        record = _get_event_record(event_id, db)
        if record:
            record.status = "succeeded"
        db.commit()

    except Exception as e:
        db.rollback()
        record = db.query(StripeEvent).filter(StripeEvent.id == event_id).first()
        if record:
            record.status = "failed"
            record.error  = str(e)
            db.commit()
        logger.exception("webhook_handler_failed", extra={
            "event_id":   event_id,
            "event_type": event_type,
            "error":      str(e),
        })
        raise  # Re-raise → 500 → Stripe retries


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


def _on_dispute_created(charge: dict, db: Session) -> None:
    """
    Stripe fires charge.dispute.created with the Charge object as `data.object`.

    Field mapping (safe extraction):
      charge["id"]               → the charge ID  (use to look up Tip.stripe_charge_id)
      charge["payment_intent"]   → the PI ID      (may be None for older charges)
      charge["dispute"]          → the dispute ID  (string on newer API, may be absent)

    We look up the tip by charge_id (set in payment_succeeded via latest_charge).
    Falling back to payment_intent lookup handles edge cases where stripe_charge_id
    wasn't stored yet.
    """
    charge_id      = charge.get("id")
    payment_intent = charge.get("payment_intent")
    dispute_id     = charge.get("dispute")
    amount_cents   = charge.get("amount", 0)

    if not charge_id:
        logger.warning("dispute_created_missing_charge_id", extra={"raw_keys": list(charge.keys())})
        return

    # Primary lookup by charge ID
    tip = db.query(Tip).filter(Tip.stripe_charge_id == charge_id).first()

    # Fallback — if stripe_charge_id not yet set, try payment_intent ID
    if not tip and payment_intent:
        tip = db.query(Tip).filter(Tip.stripe_payment_intent_id == payment_intent).first()
        if tip:
            logger.info("dispute_tip_found_via_pi_fallback", extra={
                "charge_id":      charge_id,
                "payment_intent": payment_intent,
                "tip_id":         str(tip.id),
            })

    if not tip:
        logger.warning("dispute_created_no_tip_found", extra={
            "charge_id":      charge_id,
            "payment_intent": payment_intent,
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
            creator.payout_frozen_reason = f"Dispute on charge {charge_id}"
        else:
            logger.warning("dispute_creator_not_found", extra={
                "user_id":   str(user_id),
                "charge_id": charge_id,
            })

    # Use dispute_id if present, fall back to charge_id to avoid null PK
    stripe_dispute_id = dispute_id or charge_id

    db.add(Dispute(
        tip_id            = tip_id,
        user_id           = user_id,
        stripe_dispute_id = stripe_dispute_id,
        amount_cents      = amount_cents,
    ))
    db.flush()

    logger.warning("dispute_created", extra={
        "charge_id":        charge_id,
        "payment_intent":   payment_intent,
        "dispute_id":       dispute_id,
        "tip_id":           str(tip_id) if tip_id else None,
        "user_id":          str(user_id) if user_id else None,
        "amount_cents":     amount_cents,
        "payout_frozen":    user_id is not None,
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