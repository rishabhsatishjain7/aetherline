from sqlalchemy import Column, String, DateTime, Integer, Index, func
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

    # See order-service/app/models.py: the relayer's "WHERE published = 0 ORDER
    # BY id LIMIT 50" poll needs this composite index or it degenerates into a
    # full index scan every second once the backlog is drained.
    __table_args__ = (Index("ix_outbox_events_published_id", "published", "id"),)
