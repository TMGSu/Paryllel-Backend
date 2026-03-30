import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.core.config import settings
from app.core.deps import get_db
from app.core.auth import verify_token
from app.models.user import User
from app.models.payout import Payout
from app.services import payout_service
from app.schemas.payouts import PayoutRequest, PayoutResponse, BalanceResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payouts", tags=["payouts"])


@router.get("/balance", response_model=BalanceResponse)
def get_balance(payload: dict = Depends(verify_token), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    available = payout_service.get_available_balance_cents(user.id, db)
    pending   = payout_service.get_pending_balance_cents(user.id, db)

    return BalanceResponse(
        available_cents     = available,
        pending_cents       = pending,
        threshold_cents     = settings.PAYOUT_THRESHOLD_CENTS,
        can_payout          = available >= settings.PAYOUT_THRESHOLD_CENTS and not user.payout_frozen,
        payout_frozen       = user.payout_frozen,
        onboarding_complete = user.stripe_onboarding_complete,
    )


@router.post("/request", response_model=PayoutResponse)
def request_payout(
    body: PayoutRequest,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        payout = payout_service.request_payout(user, body.amount_cents, db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return PayoutResponse(
        payout_id    = str(payout.id),
        amount_cents = payout.amount_cents,
        status       = payout.status,
    )


@router.get("/history")
def payout_history(
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
):
    user = db.query(User).filter(User.clerk_user_id == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    payouts = (
        db.query(Payout)
        .filter(Payout.user_id == user.id)
        .order_by(desc(Payout.requested_at))
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [
        {
            "payout_id":    str(p.id),
            "amount_cents": p.amount_cents,
            "status":       p.status,
            "arrival_date": int(p.paid_at.timestamp()) if p.paid_at else None,
            "created":      int(p.requested_at.timestamp()),
        }
        for p in payouts
    ]