# app/models/community_subscription_plan.py
from sqlalchemy import Column, String, Integer, Boolean, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class CommunitySubscriptionPlan(Base):
    __tablename__ = "community_subscription_plans"

    id                = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    community_id      = Column(UUID(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), nullable=False)
    price_cents       = Column(Integer, nullable=False)
    currency          = Column(String, nullable=False, server_default=text("'usd'"))
    stripe_product_id = Column(String, nullable=False)
    stripe_price_id   = Column(String, nullable=False)
    is_active         = Column(Boolean, nullable=False, server_default=text("true"))
    created_at        = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at        = Column(DateTime, nullable=False, server_default=text("NOW()"))

    community = relationship("Community", foreign_keys=[community_id])