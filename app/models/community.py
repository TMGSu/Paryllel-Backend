from sqlalchemy import Column, String, Text, Integer, Boolean, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class Community(Base):
    __tablename__ = "communities"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    name = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    icon_url = Column(Text, nullable=True)
    banner_url = Column(Text, nullable=True)
    member_count = Column(Integer, nullable=False, server_default=text("0"))
    is_nsfw = Column(Boolean, nullable=False, server_default=text("false"))
    is_private = Column(Boolean, nullable=False, server_default=text("false"))
    category = Column(String, nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at                 = Column(DateTime, nullable=False, server_default=text("NOW()"))
    subscription_enabled       = Column(Boolean, nullable=False, server_default=text("false"))
    subscription_price_cents   = Column(Integer, nullable=True)

    creator = relationship("User", foreign_keys=[created_by])
    posts = relationship("Post", back_populates="community")
    memberships = relationship("CommunityMember", back_populates="community")