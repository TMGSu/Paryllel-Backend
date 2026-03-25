from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class Media(Base):
    __tablename__ = "media"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    url = Column(String, nullable=False)
    media_type = Column(String, nullable=False)  # image | video
    content_type = Column(String, nullable=True)  # image/jpeg, video/mp4 etc
    file_size = Column(Integer, nullable=True)    # bytes
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    duration = Column(Integer, nullable=True)     # seconds, for videos
    sort_order = Column(Integer, nullable=False, server_default=text("0"))

    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    post = relationship("Post", back_populates="media")
    uploader = relationship("User", foreign_keys=[uploaded_by])