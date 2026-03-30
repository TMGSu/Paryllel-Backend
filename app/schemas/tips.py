from pydantic import BaseModel, Field
from typing import Optional

class TipRequest(BaseModel):
    amount_cents:     int = Field(..., ge=300, le=100_000)
    payment_method_id: str = Field(default="")
    idempotency_key:  str = Field(..., min_length=8, max_length=64)


class TipResponse(BaseModel):
    tip_id:           str
    status:           str
    chosen_cents:     int
    charged_cents:    int
    stripe_fee_cents: int
    message:          str = "Payment received. Your tip is being processed."

class QuoteResponse(BaseModel):
    chosen_cents:       int
    gross_cents:        int
    stripe_fee_cents:   int
    platform_fee_cents: int
    creator_cents:      int
    platform_fee_pct:   int