from sqlalchemy import Column, String, Integer, DateTime, text
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base

class Dispute(Base):
    __tablename__ = "disputes"

    id                = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    tip_id            = Column(UUID(as_uuid=True), nullable=True,  index=True)
    user_id           = Column(UUID(as_uuid=True), nullable=True,  index=True)
    stripe_dispute_id = Column(String, nullable=False, unique=True)
    amount_cents      = Column(Integer, nullable=False)
    status            = Column(String,  nullable=False, default="open")
    created_at        = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))