# app/services/fee_service.py
import math
from dataclasses import dataclass
from app.core.config import settings


@dataclass(frozen=True)
class FeeBreakdown:
    chosen_cents:        int   # tipper's selected amount
    gross_cents:         int   # actual charge to tipper's card
    stripe_fee_cents:    int   # Stripe processing fee (borne by platform)
    application_fee_cents: int # passed to application_fee_amount — covers platform + stripe fee
    platform_target_cents: int # what platform keeps after absorbing stripe fee
    creator_cents:       int   # what creator receives (exactly 80% of chosen)
    platform_net_cents:  int   # alias for platform_target_cents — platform's actual take-home
    platform_fee_pct:    int   # snapshot of rate used at tip time


def compute_fees(
    chosen_cents: int,
    platform_fee_pct: int = settings.DEFAULT_PLATFORM_FEE_PCT,
) -> FeeBreakdown:
    """
    Strict Model A — exact 80/20 split on chosen_cents, Stripe fee on top.

    FLOW:
    ─────
    1. Tipper selects chosen_cents (e.g. $5.00 = 500 cents).
    2. We gross-up so tipper also covers the Stripe processing fee:
           gross = ceil((chosen + 30) / (1 - 0.029))
    3. Platform split is computed from chosen_cents ONLY — never from gross:
           platform_target = floor(chosen * platform_fee_pct / 100)
           creator         = chosen - platform_target
    4. application_fee_amount is set so that after Stripe pulls it from the
       gross, exactly creator_cents remain in the connected account:
           application_fee_cents = gross - creator_cents
       This means application_fee covers both the Stripe fee AND the platform cut.
       Stripe deducts its fee from application_fee_cents; platform nets the rest.
    5. platform_net = application_fee_cents - stripe_fee_cents = platform_target_cents

    GUARANTEE:
    ──────────
    - Creator receives EXACTLY creator_cents (80% of chosen by default)
    - Platform nets EXACTLY platform_target_cents (20% of chosen by default)
    - Stripe fee is fully absorbed by the application_fee; tipper pays it via gross-up

    VALIDATION — chosen=500, platform_fee_pct=20:
    ──────────────────────────────────────────────
    platform_target      = floor(500 * 0.20)        = 100 cents  ($1.00)
    creator              = 500 - 100                = 400 cents  ($4.00)
    gross                = ceil(530 / 0.971)        = 546 cents  ($5.46)
    stripe_fee           = 546 - 500                =  46 cents  ($0.46)
    application_fee      = 546 - 400                = 146 cents  ($1.46)
    platform_net         = 146 - 46                 = 100 cents  ($1.00) ✓

    FUTURE TIERS:
    ─────────────
    Pass platform_fee_pct=10 for Pro creators. All values adjust automatically.
    The creator always receives exactly (100 - platform_fee_pct)% of chosen_cents.
    """
    # Step 1 — platform split from chosen (never from gross)
    platform_target_cents = math.floor(chosen_cents * platform_fee_pct / 100)
    creator_cents         = chosen_cents - platform_target_cents

    # Step 2 — gross-up so tipper covers Stripe fee
    gross_cents      = math.ceil((chosen_cents + settings.STRIPE_FIXED_CENTS) / (1 - settings.STRIPE_PCT))
    stripe_fee_cents = gross_cents - chosen_cents

    # Step 3 — application_fee locks creator's share exactly
    # Stripe routes (gross - application_fee) to creator Express account
    # platform absorbs stripe_fee from within application_fee
    application_fee_cents = gross_cents - creator_cents
    platform_net_cents    = platform_target_cents  # what platform keeps after stripe_fee

    return FeeBreakdown(
        chosen_cents          = chosen_cents,
        gross_cents           = gross_cents,
        stripe_fee_cents      = stripe_fee_cents,
        application_fee_cents = application_fee_cents,
        platform_target_cents = platform_target_cents,
        creator_cents         = creator_cents,
        platform_net_cents    = platform_net_cents,
        platform_fee_pct      = platform_fee_pct,
    )