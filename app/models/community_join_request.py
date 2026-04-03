from sqlalchemy import Column, String, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base

class CommunityJoinRequest(Base):
    __tablename__ = "community_join_requests"

    id           = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    community_id = Column(UUID(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), nullable=False)
    user_id      = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status       = Column(String(20), nullable=False, server_default=text("'pending'"))
    created_at   = Column(DateTime, nullable=False, server_default=text("NOW()"))
    reviewed_at  = Column(DateTime, nullable=True)
    reviewed_by  = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user      = relationship("User", foreign_keys=[user_id])
    community = relationship("Community", foreign_keys=[community_id])