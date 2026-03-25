from sqlalchemy import Column, Text, Integer, Boolean, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class Comment(Base):
    __tablename__ = "comments"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    author_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE"), nullable=True)  # for nested replies

    body = Column(Text, nullable=False)
    upvotes = Column(Integer, nullable=False, server_default=text("0"))
    downvotes = Column(Integer, nullable=False, server_default=text("0"))

    is_removed = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    post = relationship("Post", back_populates="comments")
    author = relationship("User", foreign_keys=[author_id])
    replies = relationship("Comment", foreign_keys=[parent_id])
    votes = relationship("Vote", back_populates="comment", cascade="all, delete-orphan")