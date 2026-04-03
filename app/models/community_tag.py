from sqlalchemy import Column, String, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base

class CommunityTag(Base):
    __tablename__ = "community_tags"

    id           = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    community_id = Column(UUID(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), nullable=False)
    name         = Column(String(50), nullable=False)
    color        = Column(String(7), nullable=True)
    created_at   = Column(DateTime, nullable=False, server_default=text("NOW()"))

    community = relationship("Community", foreign_keys=[community_id])