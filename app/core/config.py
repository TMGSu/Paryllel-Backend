# app/core/config.py
import os

class Settings:
    STRIPE_SECRET_KEY: str        = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET: str    = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    FRONTEND_URL: str             = os.getenv("FRONTEND_URL", "http://localhost:3000")
    DEFAULT_PLATFORM_FEE_PCT: int = 20
    STRIPE_PCT: float             = 0.029
    STRIPE_FIXED_CENTS: int       = 30
    PAYOUT_THRESHOLD_CENTS: int   = 2500   # $25
    PAYOUT_HOLD_DAYS: int         = 7

settings = Settings()