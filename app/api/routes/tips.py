import stripe
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.core.config import settings
from app.core.deps import get_db
from app.core.auth import verify_token
from app.models.post import Post
from app.models.user import User
from app.models.tip import Tip
from app.services import fee_service, tip_service

from app.schemas.tips import TipRequest, TipResponse, QuoteResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tips", tags=["tips"])
stripe.api_key = settings.STRIPE_SECRET_KEY


# ── Quote ─────────────────────────────────────────────────────────────────────

@router.get("/quote", response_model=QuoteResponse)
def tip_quote(
    post_id: str,
    amount_cents: int,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    if not (300 <= amount_cents <= 100_000):
        raise HTTPException(status_code=400, detail="Amount out of range ($3–$1,000)")

    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    author = db.query(User).filter(User.id == post.author_id).first()
    if not author:
        raise HTTPException(status_code=404, detail="Author not found")

    fees = fee_service.compute_fees(
        amount_cents,
        tip_service.get_creator_fee_pct(author),
    )

    return QuoteResponse(
        chosen_cents       = fees.chosen_cents,
        gross_cents        = fees.gross_cents,
        stripe_fee_cents   = fees.stripe_fee_cents,
        creator_cents      = fees.creator_cents,
        platform_fee_cents = fees.platform_target_cents,
        platform_fee_pct   = fees.platform_fee_pct,
    )

@router.post("/{post_id}/create-intent")
def create_tip_intent(
    post_id: str,
    body: TipRequest,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    tipper = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not tipper:
        raise HTTPException(status_code=404, detail="User not found")

    existing = tip_service.check_idempotency(body.idempotency_key, db)
    if existing:
        intent = stripe.PaymentIntent.retrieve(existing.stripe_payment_intent_id)
        return {"client_secret": intent.client_secret, "gross_cents": existing.gross_amount_cents}

    post   = db.query(Post).filter(Post.id == post_id).first()
    author = db.query(User).filter(User.id == post.author_id).first() if post else None

    try:
        tip_service.validate_tip(post, tipper, body.amount_cents)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not author or not author.stripe_account_id or not author.stripe_onboarding_complete:
        raise HTTPException(status_code=402, detail="This creator hasn't set up payouts yet")

    fees = fee_service.compute_fees(body.amount_cents, tip_service.get_creator_fee_pct(author))

    intent = stripe.PaymentIntent.create(
        amount   = fees.gross_cents,
        currency = "usd",
        metadata = {
            "post_id":           post_id,
            "from_user_id":      str(tipper.id),
            "to_user_id":        str(author.id),
            "chosen_cents":      str(body.amount_cents),
            "creator_stripe_id": author.stripe_account_id,
        },
    )

    tip_service.create_tip_record(
        db                = db,
        fees              = fees,
        tipper_id         = tipper.id,
        author_id         = author.id,
        post_id           = post.id,
        payment_intent_id = intent.id,
        idempotency_key   = body.idempotency_key,
    )
    db.commit()

    return {"client_secret": intent.client_secret, "gross_cents": fees.gross_cents}

# ── Stripe Connect onboarding ─────────────────────────────────────────────────

@router.post("/connect/onboard")
def start_connect_onboarding(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.stripe_account_id:
        account = stripe.Account.create(
            type     = "express",
            metadata = {"paryllel_user_id": str(user.id)},
        )
        user.stripe_account_id          = account.id
        user.stripe_onboarding_complete = False
        db.commit()

    account_link = stripe.AccountLink.create(
        account     = user.stripe_account_id,
        refresh_url = f"{settings.FRONTEND_URL}/earnings?onboard=refresh",
        return_url  = f"{settings.FRONTEND_URL}/earnings?onboard=complete",
        type        = "account_onboarding",
    )
    return {"url": account_link.url}


@router.get("/connect/status")
def connect_status(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.stripe_account_id:
        return {"connected": False, "onboarding_complete": False}

    account  = stripe.Account.retrieve(user.stripe_account_id)
    complete = account.details_submitted or False

    if complete and not user.stripe_onboarding_complete:
        user.stripe_onboarding_complete = True
        db.commit()

    return {
        "connected":           True,
        "onboarding_complete": complete,
        "stripe_account_id":   user.stripe_account_id,
    }


# ── Send a tip ────────────────────────────────────────────────────────────────

@router.post("/{post_id}", response_model=TipResponse)
def send_tip(
    post_id: str,
    body: TipRequest,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    tipper = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not tipper:
        raise HTTPException(status_code=404, detail="User not found")

    existing = tip_service.check_idempotency(body.idempotency_key, db)
    if existing:
        return TipResponse(
            tip_id           = str(existing.id),
            status           = existing.status,
            chosen_cents     = existing.chosen_amount_cents,
            charged_cents    = existing.gross_amount_cents,
            stripe_fee_cents = existing.stripe_fee_cents,
            message          = "This tip was already submitted.",
        )

    post   = db.query(Post).filter(Post.id == post_id).first()
    author = db.query(User).filter(User.id == post.author_id).first() if post else None

    try:
        tip_service.validate_tip(post, tipper, body.amount_cents)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not author or not author.stripe_account_id or not author.stripe_onboarding_complete:
        raise HTTPException(status_code=402, detail="This creator hasn't set up payouts yet")

    fees = fee_service.compute_fees(body.amount_cents, tip_service.get_creator_fee_pct(author))

    try:
        intent = tip_service.create_payment_intent(
            fees              = fees,
            payment_method_id = body.payment_method_id,
            creator_stripe_id = author.stripe_account_id,
            idempotency_key   = body.idempotency_key,
            metadata          = {
                "post_id":      post_id,
                "from_user_id": str(tipper.id),
                "to_user_id":   str(author.id),
                "chosen_cents": str(body.amount_cents),
            },
        )
    except stripe.error.CardError as e:
        raise HTTPException(status_code=402, detail=str(e.user_message))
    except stripe.error.StripeError:
        logger.exception("Stripe error creating tip")
        raise HTTPException(status_code=500, detail="Payment failed. Try again.")

    tip = tip_service.create_tip_record(
        db                = db,
        fees              = fees,
        tipper_id         = tipper.id,
        author_id         = author.id,
        post_id           = post.id,
        payment_intent_id = intent.id,
        idempotency_key   = body.idempotency_key,
    )
    db.commit()

    return TipResponse(
        tip_id           = str(tip.id),
        status           = "processing",
        chosen_cents     = fees.chosen_cents,
        charged_cents    = fees.gross_cents,
        stripe_fee_cents = fees.stripe_fee_cents,
    )
    



# ── Tip history ───────────────────────────────────────────────────────────────

@router.get("/history/received")
def tip_history_received(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
):
    user = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tips = (
        db.query(Tip)
        .filter(Tip.to_user_id == user.id)
        .order_by(desc(Tip.created_at))
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [
        {
            "tip_id":        str(t.id),
            "post_id":       str(t.post_id),
            "from_user_id":  str(t.from_user_id),
            "gross_cents":   t.gross_amount_cents,
            "creator_cents": t.creator_amount_cents,
            "status":        t.status,
            "created_at":    t.created_at,
        }
        for t in tips
    ]


@router.get("/history/sent")
def tip_history_sent(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
):
    user = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tips = (
        db.query(Tip)
        .filter(Tip.from_user_id == user.id)
        .order_by(desc(Tip.created_at))
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [
        {
            "tip_id":      str(t.id),
            "post_id":     str(t.post_id),
            "to_user_id":  str(t.to_user_id),
            "gross_cents": t.gross_amount_cents,
            "status":      t.status,
            "created_at":  t.created_at,
        }
        for t in tips
    ]