import uuid
from sqlalchemy import Column, String, Integer, Boolean, DateTime, Index, text
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base

class Tip(Base):
    __tablename__ = "tips"

    id                       = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    from_user_id             = Column(UUID(as_uuid=True), nullable=False, index=True)
    to_user_id               = Column(UUID(as_uuid=True), nullable=False, index=True)
    post_id                  = Column(UUID(as_uuid=True), nullable=True,  index=True)

    # All in cents — stored separately for full auditability
    chosen_amount_cents      = Column(Integer, nullable=False)  # what tipper selected
    gross_amount_cents       = Column(Integer, nullable=False)  # actual charge (chosen + stripe fee)
    stripe_fee_cents         = Column(Integer, nullable=False)  # gross - chosen
    platform_fee_cents       = Column(Integer, nullable=False)  # floor(chosen * fee_pct/100)
    creator_amount_cents     = Column(Integer, nullable=False)  # chosen - platform_fee
    platform_fee_pct         = Column(Integer, nullable=False)  # snapshot of rate at tip time

    # Stripe references
    stripe_payment_intent_id = Column(String, unique=True, nullable=True,  index=True)
    stripe_charge_id         = Column(String, nullable=True,  index=True)
    stripe_transfer_id       = Column(String, nullable=True)
    idempotency_key          = Column(String, unique=True, nullable=False, index=True)

    # Lifecycle: created → completed → disputed / failed
    status                   = Column(String, nullable=False, default="created", index=True)

    # Payout eligibility
    available_at             = Column(DateTime(timezone=True), nullable=True)
    is_disputed              = Column(Boolean, nullable=False, default=False)
    disputed_at              = Column(DateTime(timezone=True), nullable=True)

    created_at               = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at               = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __table_args__ = (
        Index("ix_tips_to_user_status",    "to_user_id", "status"),
        Index("ix_tips_to_user_available", "to_user_id", "available_at"),
    )