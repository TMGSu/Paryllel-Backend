import os
import stripe
import logging
from fastapi import APIRouter, Request, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models.wallet import Wallet

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/stripe")
async def stripe_webhook(request: Request):
    payload   = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Get a DB session manually — no Depends() in a raw endpoint
    from app.core.deps import SessionLocal
    db: Session = SessionLocal()
    try:
        _handle_event(event, db)
    except Exception as e:
        logger.error(f"Webhook handler error: {e}", exc_info=True)
    finally:
        db.close()

    return {"received": True}


def _handle_event(event: dict, db: Session):
    event_type = event["type"]
    data       = event["data"]["object"]

    if event_type == "payment_intent.succeeded":
        _on_payment_succeeded(data)

    elif event_type == "payment_intent.payment_failed":
        _on_payment_failed(data)

    elif event_type == "transfer.created":
        logger.info(f"Transfer created: {data['id']} → {data['destination']}")

    elif event_type == "account.updated":
        _on_account_updated(data, db)

    else:
        logger.debug(f"Unhandled Stripe event: {event_type}")


def _on_payment_succeeded(pi: dict):
    """
    Tip crediting happens synchronously in POST /tips/{post_id} after
    the PaymentIntent confirms. This is a safety net for async flows.
    """
    m = pi.get("metadata", {})
    logger.info(
        f"PaymentIntent succeeded: {pi['id']} | "
        f"post={m.get('post_id')} tipper={m.get('from_user_id')} "
        f"author={m.get('to_user_id')} amount={pi['amount']}"
    )


def _on_payment_failed(pi: dict):
    logger.warning(
        f"PaymentIntent failed: {pi['id']} | "
        f"reason={pi.get('last_payment_error', {}).get('message')}"
    )


def _on_account_updated(account: dict, db: Session):
    """Auto-mark onboarding complete when Stripe fires account.updated."""
    if not account.get("details_submitted"):
        return

    wallet = (
        db.query(Wallet)
        .filter(Wallet.stripe_account_id == account["id"])
        .first()
    )
    if wallet and wallet.stripe_onboarding_complete != "true":
        wallet.stripe_onboarding_complete = "true"
        db.commit()
        logger.info(f"Connect onboarding complete: wallet user_id={wallet.user_id}")