from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class Tip(Base):
    __tablename__ = "tips"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    from_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    to_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="SET NULL"), nullable=True)

    amount = Column(Integer, nullable=False)  # in cents e.g. 500 = $5.00
    currency = Column(String, nullable=False, server_default=text("'usd'"))
    status = Column(String, nullable=False, server_default=text("'pending'"))  # pending | completed | failed | refunded
    stripe_payment_intent_id = Column(String, nullable=True)  # for when you add Stripe

    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    sender = relationship("User", foreign_keys=[from_user_id])
    recipient = relationship("User", foreign_keys=[to_user_id])
    post = relationship("Post", back_populates="tips")