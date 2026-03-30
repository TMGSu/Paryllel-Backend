from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, text
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base

class Payout(Base):
    __tablename__ = "payouts"

    id               = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id          = Column(UUID(as_uuid=True), nullable=False, index=True)
    amount_cents     = Column(Integer, nullable=False)
    stripe_payout_id = Column(String, nullable=True, index=True)
    status           = Column(String, nullable=False, default="requested")  # requested | paid | failed
    failure_reason   = Column(Text,   nullable=True)
    requested_at     = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    paid_at          = Column(DateTime(timezone=True), nullable=True)