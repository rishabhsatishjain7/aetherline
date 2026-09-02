from sqlalchemy import Column, String, Integer, DateTime, UniqueConstraint, func
from .database import Base


class Notification(Base):
    """Unique constraint on (order_id, notification_type) is the idempotency guard --
    a redelivered event for an order already notified fails the constraint instead
    of sending (recording) a duplicate notification."""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String, nullable=False)
    notification_type = Column(String, nullable=False)  # ORDER_CONFIRMED | ORDER_FAILED
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("order_id", "notification_type", name="uq_order_notification_type"),)
