from pydantic import BaseModel, Field
from typing import Optional

class PayoutRequest(BaseModel):
    amount_cents: int = Field(..., ge=2500)

class PayoutResponse(BaseModel):
    payout_id:    str
    amount_cents: int
    status:       str
    arrival_date: Optional[int] = None

class BalanceResponse(BaseModel):
    available_cents:     int
    pending_cents:       int
    threshold_cents:     int
    can_payout:          bool
    payout_frozen:       bool
    onboarding_complete: bool