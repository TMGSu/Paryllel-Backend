from sqlalchemy import Column, String, Text, Integer, Boolean, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class Post(Base):
    __tablename__ = "posts"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    author_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    community_id = Column(UUID(as_uuid=True), ForeignKey("communities.id", ondelete="CASCADE"), nullable=False)

    title = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=True, index=True)
    body = Column(Text, nullable=True)
    post_type = Column(String, nullable=False, server_default=text("'text'"))  # text | image | video | link

    upvotes = Column(Integer, nullable=False, server_default=text("0"))
    downvotes = Column(Integer, nullable=False, server_default=text("0"))
    comment_count = Column(Integer, nullable=False, server_default=text("0"))
    tip_count = Column(Integer, nullable=False, server_default=text("0"))
    total_tips = Column(Integer, nullable=False, server_default=text("0"))  # stored in cents

    is_pinned = Column(Boolean, nullable=False, server_default=text("false"))
    is_locked = Column(Boolean, nullable=False, server_default=text("false"))
    is_removed = Column(Boolean, nullable=False, server_default=text("false"))
    is_nsfw         = Column(Boolean, nullable=False, server_default=text("false"))
    subscriber_only = Column(Boolean, nullable=False, server_default=text("false"))

    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    author = relationship("User", foreign_keys=[author_id])
    community = relationship("Community", back_populates="posts")
    media = relationship("Media", back_populates="post", cascade="all, delete-orphan")
    comments = relationship("Comment", back_populates="post", cascade="all, delete-orphan")
    votes = relationship("Vote", back_populates="post", cascade="all, delete-orphan")
