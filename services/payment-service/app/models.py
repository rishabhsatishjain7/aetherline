from sqlalchemy import Column, String, DateTime, Integer, func
from .database import Base


class Payment(Base):
    """One row per order -- the (order_id) primary key IS the idempotency guard:
    a redelivered 'inventory.reserved' message for an already-processed order
    is rejected by the DB's primary key constraint before any payment logic runs."""
    __tablename__ = "payments"

    order_id = Column(String, primary_key=True)
    status = Column(String, nullable=False)  # SUCCEEDED | FAILED
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    topic = Column(String, nullable=False)
    key = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    payload = Column(String, nullable=False)
    published = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
