from sqlalchemy import Column, String, DateTime, Text, Boolean, UniqueConstraint, text
from app.core.database import Base


class StripeEvent(Base):
    __tablename__ = "stripe_events"

    # ── Existing fields (unchanged) ─────────────────────────────────────────
    id           = Column(String, primary_key=True)   # Stripe event ID e.g. evt_xxx
    event_type   = Column(String, nullable=False)
    status       = Column(String, nullable=False, default="processing")  # processing | succeeded | failed
    error        = Column(Text,   nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    # ── NEW: Ordering & replay-attack defence ───────────────────────────────
    # Stripe's own `created` Unix timestamp converted to a tz-aware datetime.
    # Used to detect stale/out-of-order events (e.g. don't let a late
    # `payment_intent.created` overwrite a `payment_intent.succeeded`).
    created_at   = Column(DateTime(timezone=True), nullable=True)

    # ── NEW: Audit / debugging ──────────────────────────────────────────────
    # Distinguish production events from test-mode events.
    # A livemode=False event should never touch real money — log and bail.
    livemode     = Column(Boolean, nullable=False, default=False)

    # Full raw JSON payload from Stripe, stored as-is.
    # Invaluable for post-mortem debugging of financial anomalies without
    # needing to call Stripe's API after the fact.
    raw_payload  = Column(Text, nullable=True)

    # ── NEW: DB-level uniqueness constraint ─────────────────────────────────
    # The primary key alone is not enough under concurrent workers — two
    # processes can both pass the application-level "does this exist?" check
    # before either has committed.  This constraint is the final safety net:
    # the second INSERT will raise IntegrityError, which the caller catches
    # and treats as a duplicate, preventing any double processing.
    __table_args__ = (
        UniqueConstraint("id", name="uq_stripe_event_id"),
    )