import uuid
from sqlalchemy import Column, Text, Integer, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from app.core.database import Base


class CommunityWidget(Base):
    __tablename__ = "community_widgets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    community_id = Column(UUID(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), nullable=False)
    widget_type = Column(Text, nullable=False)
    title = Column(Text, nullable=True)
    position = Column(Integer, nullable=False, default=0)
    config = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)