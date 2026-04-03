from sqlalchemy import Column, String, DateTime, text
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base

class CommunityCategory(Base):
    __tablename__ = "community_categories"

    id         = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    name       = Column(String(100), nullable=False, unique=True)
    slug       = Column(String(100), nullable=False, unique=True)
    icon       = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))