from sqlalchemy import Column, String, Text, Integer, Boolean, Numeric, DateTime, text
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    clerk_user_id = Column(String, unique=True, nullable=False, index=True)

    username = Column(String, unique=True, nullable=True, index=True)
    email = Column(String, unique=True, nullable=True)

    display_name = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    avatar_url = Column(Text, nullable=True)
    banner_url = Column(Text, nullable=True)  # ← add here

    reputation = Column(Integer, nullable=False, server_default=text("0"))
    total_earned = Column(Numeric(10, 2), nullable=False, server_default=text("0"))

    is_verified = Column(Boolean, nullable=False, server_default=text("false"))
    is_banned = Column(Boolean, nullable=False, server_default=text("false"))

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=False), nullable=False, server_default=text("NOW()"))