import uuid
from sqlalchemy import Column, Text, Integer, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.core.database import Base


class CommunityRule(Base):
    __tablename__ = "community_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    community_id = Column(UUID(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)