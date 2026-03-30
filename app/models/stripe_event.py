from sqlalchemy import Column, String, DateTime, Text, text
from app.core.database import Base

class StripeEvent(Base):
    __tablename__ = "stripe_events"

    id           = Column(String, primary_key=True)   # Stripe event ID e.g. evt_xxx
    event_type   = Column(String, nullable=False)
    status       = Column(String, nullable=False, default="processing")  # processing | succeeded | failed
    error        = Column(Text,   nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

