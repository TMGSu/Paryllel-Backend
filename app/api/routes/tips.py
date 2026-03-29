import math
import os
import stripe
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel

from app.core.deps import get_db
from app.core.auth import verify_token
from app.models.tip import Tip
from app.models.post import Post
from app.models.user import User
from app.models.wallet import Wallet
from app.models.withdrawal import WithdrawalRequest

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
FRONTEND_URL  = os.getenv("FRONTEND_URL", "https://paryllel.com")

PLATFORM_CUT  = 0.20   # 20% taken at withdrawal, not at tip time
STRIPE_PCT    = 0.029  # 2.9% + $0.30
STRIPE_FIXED  = 30     # cents

VALID_PRESET_CENTS = {300, 500, 1000}  # $3, $5, $10
MIN_CUSTOM_CENTS   = 300
MAX_CUSTOM_CENTS   = 100_000

router = APIRouter(prefix="/tips", tags=["tips"])


# ─── Fee math ─────────────────────────────────────────────────────────────────

def compute_gross(chosen_cents: int) -> dict:
    """
    Tipper pays chosen amount + Stripe fee only.
    Paryllel takes 20% at withdrawal time, not here.

        gross = ceil((chosen + 30) / (1 - 0.029))
              = ceil((chosen + 30) / 0.971)

    Example — tipper picks $3 (300 cents):
        gross      = ceil(330 / 0.971) = 340 cents ($3.40 charged)
        stripe_fee = ceil(340 * 0.029) + 30 = 40 cents
        creator gets 300 cents into wallet
        Paryllel takes 20% = $0.60 when creator withdraws
    """
    divisor     = 1 - STRIPE_PCT
    gross       = math.ceil((chosen_cents + STRIPE_FIXED) / divisor)
    stripe_fee  = math.ceil(gross * STRIPE_PCT) + STRIPE_FIXED
    creator_net = gross - stripe_fee
    return {
        "chosen_cents":       chosen_cents,
        "gross_cents":        gross,
        "stripe_fee_cents":   stripe_fee,
        "platform_fee_cents": 0,
        "creator_net_cents":  creator_net,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_user(clerk_user_id: str, db: Session) -> User:
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def get_or_create_wallet(user_id, db: Session) -> Wallet:
    wallet = db.query(Wallet).filter(Wallet.user_id == str(user_id)).first()
    if not wallet:
        wallet = Wallet(user_id=str(user_id))
        db.add(wallet)
        db.commit()
        db.refresh(wallet)
    return wallet


def validate_tip_amount(amount_cents: int):
    if amount_cents not in VALID_PRESET_CENTS and amount_cents < MIN_CUSTOM_CENTS:
        raise HTTPException(status_code=400, detail="Minimum tip is $3.00")
    if amount_cents > MAX_CUSTOM_CENTS:
        raise HTTPException(status_code=400, detail="Maximum tip is $1,000")


# ─── Schemas ──────────────────────────────────────────────────────────────────

class TipRequest(BaseModel):
    amount_cents: int        # preset (300/500/1000) or custom >= 300
    payment_method_id: str   # Stripe PaymentMethod ID from frontend Elements


class WithdrawRequest(BaseModel):
    amount_cents: int


# ─── Quote ────────────────────────────────────────────────────────────────────

@router.get("/quote")
def tip_quote(amount_cents: int):
    """
    Call on amount selection so UI can show:
      'You pay $X.XX — creator receives $Y.YY'
    """
    validate_tip_amount(amount_cents)
    return compute_gross(amount_cents)


# ─── Stripe Connect onboarding ────────────────────────────────────────────────

@router.post("/connect/onboard")
def start_connect_onboarding(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    user = get_user(payload["sub"], db)
    wallet = get_or_create_wallet(user.id, db)

    if not wallet.stripe_account_id:
        account = stripe.Account.create(
            type="express",
            metadata={"paryllel_user_id": str(user.id)},
        )
        wallet.stripe_account_id = account.id
        wallet.stripe_onboarding_complete = "false"
        db.commit()

    account_link = stripe.AccountLink.create(
        account=wallet.stripe_account_id,
        refresh_url=f"{FRONTEND_URL}/earnings?onboard=refresh",
        return_url=f"{FRONTEND_URL}/earnings?onboard=complete",
        type="account_onboarding",
    )
    return {"url": account_link.url}


@router.get("/connect/status")
def connect_status(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    user = get_user(payload["sub"], db)
    wallet = get_or_create_wallet(user.id, db)

    if not wallet.stripe_account_id:
        return {"connected": False, "onboarding_complete": False}

    account = stripe.Account.retrieve(wallet.stripe_account_id)
    complete = account.get("details_submitted", False)

    if complete and wallet.stripe_onboarding_complete != "true":
        wallet.stripe_onboarding_complete = "true"
        db.commit()

    return {
        "connected": True,
        "onboarding_complete": complete,
        "stripe_account_id": wallet.stripe_account_id,
    }


# ─── Send a tip ───────────────────────────────────────────────────────────────

@router.post("/{post_id}")
def send_tip(
    post_id: str,
    body: TipRequest,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    validate_tip_amount(body.amount_cents)

    user = get_user(payload["sub"], db)

    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if str(post.author_id) == str(user.id):
        raise HTTPException(status_code=400, detail="Cannot tip your own post")

    fees          = compute_gross(body.amount_cents)
    charge_amount = fees["gross_cents"]
    creator_net   = fees["creator_net_cents"]

    try:
        intent = stripe.PaymentIntent.create(
            amount=charge_amount,
            currency="usd",
            payment_method=body.payment_method_id,
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            metadata={
                "post_id":       post_id,
                "from_user_id":  str(user.id),
                "to_user_id":    str(post.author_id),
                "chosen_cents":  str(body.amount_cents),
                "creator_net":   str(creator_net),
            },
        )
    except stripe.error.CardError as e:
        raise HTTPException(status_code=402, detail=str(e.user_message))
    except stripe.error.StripeError:
        raise HTTPException(status_code=500, detail="Payment failed. Try again.")

    if intent.status != "succeeded":
        raise HTTPException(status_code=402, detail="Payment not completed")

    tip = Tip(
        from_user_id=user.id,
        to_user_id=post.author_id,
        post_id=post.id,
        amount=charge_amount,
        currency="usd",
        status="completed",
        stripe_payment_intent_id=intent.id,
    )
    db.add(tip)

    author_wallet = get_or_create_wallet(post.author_id, db)
    author_wallet.balance      += creator_net
    author_wallet.total_earned += creator_net

    post.total_tips = (post.total_tips or 0) + charge_amount

    db.commit()
    db.refresh(tip)

    return {
        "tip_id":             str(tip.id),
        "chosen_cents":       body.amount_cents,
        "charged_cents":      charge_amount,
        "stripe_fee_cents":   fees["stripe_fee_cents"],
        "platform_fee_cents": fees["platform_fee_cents"],
        "creator_net_cents":  creator_net,
        "status":             "success",
    }


# ─── Wallet ───────────────────────────────────────────────────────────────────

@router.get("/wallet")
def get_wallet(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    user = get_user(payload["sub"], db)
    wallet = get_or_create_wallet(user.id, db)

    pending_rows = (
        db.query(WithdrawalRequest)
        .filter(
            WithdrawalRequest.user_id == str(user.id),
            WithdrawalRequest.status == "pending",
        )
        .with_entities(WithdrawalRequest.amount)
        .all()
    )
    pending_total = sum(r.amount for r in pending_rows)

    return {
        "balance_cents":            wallet.balance,
        "available_cents":          wallet.balance - pending_total,
        "pending_withdrawal_cents": pending_total,
        "total_earned_cents":       wallet.total_earned,
        "total_withdrawn_cents":    wallet.total_withdrawn,
        "stripe_connected":         bool(wallet.stripe_account_id),
        "onboarding_complete":      wallet.stripe_onboarding_complete == "true",
    }


@router.get("/history/received")
def tip_history_received(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
):
    user = get_user(payload["sub"], db)
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
            "tip_id":            str(t.id),
            "post_id":           str(t.post_id),
            "from_user_id":      str(t.from_user_id),
            "gross_cents":       t.amount,
            "creator_net_cents": compute_gross(t.amount)["creator_net_cents"],
            "status":            t.status,
            "created_at":        t.created_at,
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
    user = get_user(payload["sub"], db)
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
            "gross_cents": t.amount,
            "status":      t.status,
            "created_at":  t.created_at,
        }
        for t in tips
    ]


# ─── Withdrawal requests ──────────────────────────────────────────────────────

@router.post("/withdraw/request")
def request_withdrawal(
    body: WithdrawRequest,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    if body.amount_cents < 2500:
        raise HTTPException(status_code=400, detail="Minimum withdrawal is $25.00")

    user = get_user(payload["sub"], db)
    wallet = get_or_create_wallet(user.id, db)

    if wallet.stripe_onboarding_complete != "true":
        raise HTTPException(
            status_code=400,
            detail="Complete Stripe onboarding before withdrawing",
        )

    if wallet.balance < 2500:
        raise HTTPException(
            status_code=400,
            detail="Balance must be at least $25.00 to withdraw",
        )

    if body.amount_cents > wallet.balance:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    # Paryllel takes 20% at withdrawal
    PLATFORM_CUT = 0.20
    payout_amount = int(body.amount_cents * (1 - PLATFORM_CUT))

    # Fire Stripe transfer immediately
    try:
        transfer = stripe.Transfer.create(
            amount=payout_amount,
            currency="usd",
            destination=wallet.stripe_account_id,
            metadata={
                "paryllel_user_id":   str(user.id),
                "requested_cents":    str(body.amount_cents),
                "platform_fee_cents": str(body.amount_cents - payout_amount),
            },
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=500, detail=f"Transfer failed: {str(e)}")

    # Deduct from wallet and record
    wallet.balance         -= body.amount_cents
    wallet.total_withdrawn += payout_amount

    req = WithdrawalRequest(
        user_id=str(user.id),
        amount=body.amount_cents,
        status="paid",
        stripe_transfer_id=transfer.id,
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    return {
        "withdrawal_id":      req.id,
        "requested_cents":    body.amount_cents,
        "payout_cents":       payout_amount,
        "platform_fee_cents": body.amount_cents - payout_amount,
        "stripe_transfer_id": transfer.id,
        "status":             "paid",
        "created_at":         req.created_at,
    }


@router.get("/withdraw/history")
def withdrawal_history(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    user = get_user(payload["sub"], db)
    reqs = (
        db.query(WithdrawalRequest)
        .filter(WithdrawalRequest.user_id == str(user.id))
        .order_by(desc(WithdrawalRequest.created_at))
        .all()
    )
    return [
        {
            "id":           r.id,
            "amount_cents": r.amount,
            "status":       r.status,
            "created_at":   r.created_at,
            "reviewed_at":  r.reviewed_at,
            "admin_note":   r.admin_note,
        }
        for r in reqs
    ]