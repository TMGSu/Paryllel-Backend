from sqlalchemy import Column, Boolean, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class CommunityMember(Base):
    __tablename__ = "community_members"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    community_id = Column(UUID(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), nullable=False)
    is_moderator = Column(Boolean, nullable=False, server_default=text("false"))
    joined_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    user = relationship("User")
    community = relationship("Community", back_populates="memberships")