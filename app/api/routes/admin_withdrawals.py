import os
import stripe
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone

from app.core.deps import get_db
from app.core.auth import verify_token
from app.models.wallet import Wallet
from app.models.withdrawal import WithdrawalRequest

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

ADMIN_CLERK_ID = "user_3BSBhTe2TZtLQAFZ51BQzJZEZ6B"

router = APIRouter(prefix="/admin/withdrawals", tags=["admin-withdrawals"])


def require_admin(payload: dict):
    if payload.get("sub") != ADMIN_CLERK_ID:
        raise HTTPException(status_code=403, detail="Forbidden")


class ReviewBody(BaseModel):
    action: str                    # "approve" or "reject"
    admin_note: Optional[str] = None


@router.get("")
def list_withdrawals(
    status: Optional[str] = None,  # pending | rejected | paid
    skip: int = 0,
    limit: int = 50,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)

    q = db.query(WithdrawalRequest)
    if status:
        q = q.filter(WithdrawalRequest.status == status)
    reqs = q.order_by(desc(WithdrawalRequest.created_at)).offset(skip).limit(limit).all()

    results = []
    for r in reqs:
        wallet = db.query(Wallet).filter(Wallet.user_id == r.user_id).first()
        results.append({
            "id":                   r.id,
            "user_id":              r.user_id,
            "amount_cents":         r.amount,
            "status":               r.status,
            "stripe_transfer_id":   r.stripe_transfer_id,
            "admin_note":           r.admin_note,
            "reviewed_by":          r.reviewed_by,
            "reviewed_at":          r.reviewed_at,
            "created_at":           r.created_at,
            "stripe_account_id":    wallet.stripe_account_id if wallet else None,
            "wallet_balance_cents": wallet.balance if wallet else 0,
        })
    return results


@router.patch("/{withdrawal_id}")
def review_withdrawal(
    withdrawal_id: str,
    body: ReviewBody,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
):
    require_admin(payload)

    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'")

    req = db.query(WithdrawalRequest).filter(WithdrawalRequest.id == withdrawal_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Withdrawal not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already {req.status}")

    wallet = db.query(Wallet).filter(Wallet.user_id == req.user_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="User wallet not found")

    now = datetime.now(timezone.utc)

    if body.action == "reject":
        req.status     = "rejected"
        req.admin_note = body.admin_note
        req.reviewed_by = payload["sub"]
        req.reviewed_at = now
        db.commit()
        return {"status": "rejected", "withdrawal_id": req.id}

    # Approve — trigger Stripe transfer to creator's Express account
    if not wallet.stripe_account_id:
        raise HTTPException(status_code=400, detail="User has no connected Stripe account")

    if wallet.balance < req.amount:
        raise HTTPException(status_code=400, detail="User wallet balance insufficient")

    try:
        transfer = stripe.Transfer.create(
            amount=req.amount,
            currency="usd",
            destination=wallet.stripe_account_id,
            metadata={
                "withdrawal_request_id": req.id,
                "paryllel_user_id":      req.user_id,
            },
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=500, detail=f"Stripe transfer failed: {str(e)}")

    wallet.balance         -= req.amount
    wallet.total_withdrawn += req.amount

    req.status           = "paid"
    req.stripe_transfer_id = transfer.id
    req.admin_note       = body.admin_note
    req.reviewed_by      = payload["sub"]
    req.reviewed_at      = now

    db.commit()

    return {
        "status":             "paid",
        "withdrawal_id":      req.id,
        "stripe_transfer_id": transfer.id,
        "amount_cents":       req.amount,
    }