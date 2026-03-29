import uuid
from sqlalchemy import Column, Text, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.core.database import Base


class PostReport(Base):
    __tablename__ = "post_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    community_id = Column(UUID(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), nullable=False)
    reported_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default="pending")  # pending | resolved | dismissed
    resolved_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)