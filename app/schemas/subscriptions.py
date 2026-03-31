# app/schemas/subscriptions.py
from datetime import datetime
from pydantic import BaseModel, field_validator


class SubscriptionSetupRequest(BaseModel):
    price_cents: int  # e.g. 499 = $4.99/month

    @field_validator("price_cents")
    @classmethod
    def must_be_positive(cls, v):
        if v < 100:
            raise ValueError("Minimum subscription price is $1.00 (100 cents).")
        return v


class SubscriptionSetupResponse(BaseModel):
    plan_id:         str
    price_cents:     int
    stripe_price_id: str
    is_active:       bool


class SubscribeRequest(BaseModel):
    payment_method_id: str  # Stripe PaymentMethod ID from the frontend

    @field_validator("payment_method_id")
    @classmethod
    def must_be_stripe_pm(cls, v):
        if not v.startswith("pm_"):
            raise ValueError("payment_method_id must be a valid Stripe PaymentMethod ID.")
        return v


class SubscribeResponse(BaseModel):
    subscription_id:    str
    status:             str  # active | incomplete | past_due
    current_period_end: datetime


class CancelResponse(BaseModel):
    subscription_id:    str
    status:             str
    current_period_end: datetime
    message:            str


class SubscriptionStatusResponse(BaseModel):
    subscriptions_enabled: bool
    price_cents:           int | None
    is_subscribed:         bool
    status:                str | None  # active | past_due | canceled | None
    current_period_end:    datetime | None