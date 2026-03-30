from sqlalchemy import Column, String, Integer, DateTime, Text, text
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base

class BalanceEntry(Base):
    __tablename__ = "balance_entries"

    id           = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id      = Column(UUID(as_uuid=True), nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)            # positive = credit, negative = debit
    entry_type   = Column(String,  nullable=False)            # tip_received | payout_requested | payout_reversed | adjustment
    reference_id = Column(UUID(as_uuid=True), nullable=True)  # tip.id or payout.id
    note         = Column(Text,    nullable=True)
    created_at   = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))