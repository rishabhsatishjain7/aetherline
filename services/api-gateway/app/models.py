import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, func

from .database import Base


class User(Base):
    """A human user of the gateway (per-user JWT auth, Phase 1)."""

    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class OrderOwnership(Base):
    """Which gateway user owns which order (Phase 2 authorization)."""

    __tablename__ = "order_ownership"

    order_id = Column(String(255), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

