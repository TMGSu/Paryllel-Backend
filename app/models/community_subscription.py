# app/models/community_subscription.py
from sqlalchemy import Column, String, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class CommunitySubscription(Base):
    __tablename__ = "community_subscriptions"

    id                     = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    community_id           = Column(UUID(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), nullable=False)
    subscriber_user_id     = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    stripe_subscription_id = Column(String, nullable=False, unique=True)
    stripe_customer_id     = Column(String, nullable=False)
    # active | past_due | canceled | incomplete
    status                 = Column(String, nullable=False, server_default=text("'active'"))
    current_period_end     = Column(DateTime, nullable=False)
    created_at             = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at             = Column(DateTime, nullable=False, server_default=text("NOW()"))

    community  = relationship("Community", foreign_keys=[community_id])
    subscriber = relationship("User", foreign_keys=[subscriber_user_id])