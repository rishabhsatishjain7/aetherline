import uuid

from sqlalchemy import Column, DateTime, String, func

from .database import Base


class User(Base):
    """A human user of the gateway (per-user JWT auth, Phase 1).

    Lives in gateway-db -- the gateway's own database, same
    database-per-service pattern as the other four services. Only the
    gateway reads/writes this table; the upstream services never see it.
    """

    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Stored lower-cased/trimmed by the register/login schema so uniqueness
    # (and login) is effectively case-insensitive; the DB constraint is what
    # actually enforces it -- see the IntegrityError handling in main.py.
    email = Column(String(255), unique=True, nullable=False, index=True)
    # bcrypt hash (cost factor embedded in the string), e.g. $2b$12$...
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
