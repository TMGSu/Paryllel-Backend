import uuid
from sqlalchemy import Column, String, BigInteger, DateTime, Text, func
from app.core.database import Base


class WithdrawalRequest(Base):
    __tablename__ = "withdrawal_requests"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=False, index=True)

    # Amount in cents
    amount = Column(BigInteger, nullable=False)

    # pending | approved | rejected | paid
    status = Column(String, nullable=False, default="pending")

    # Set when admin approves and Stripe transfer is initiated
    stripe_transfer_id = Column(String, nullable=True)

    # Admin notes on rejection
    admin_note = Column(Text, nullable=True)

    # Which admin acted on it
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())