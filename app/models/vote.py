from sqlalchemy import Column, Integer, DateTime, ForeignKey, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class Vote(Base):
    __tablename__ = "votes"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=True)
    comment_id = Column(UUID(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE"), nullable=True)
    value = Column(Integer, nullable=False)  # 1 = upvote, -1 = downvote
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    user = relationship("User")
    post = relationship("Post", back_populates="votes")
    comment = relationship("Comment", back_populates="votes")

    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_vote_user_post"),
        UniqueConstraint("user_id", "comment_id", name="uq_vote_user_comment"),
    )