import enum
import uuid
from sqlalchemy import Column, String, Integer, Enum, ForeignKey, DateTime, Text, func
from sqlalchemy.orm import relationship
from .database import Base


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"      # created, waiting on inventory/payment
    CONFIRMED = "CONFIRMED"  # inventory reserved AND payment succeeded
    CANCELLED = "CANCELLED"  # explicitly cancelled by the client
    FAILED = "FAILED"        # inventory reservation or payment failed


class Order(Base):
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.PENDING)
    idempotency_key = Column(String, unique=True, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    events = relationship(
        "OrderEvent", back_populates="order", cascade="all, delete-orphan",
        order_by="OrderEvent.created_at",
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String, ForeignKey("orders.id"), nullable=False)
    sku = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)

    order = relationship("Order", back_populates="items")


class OrderEvent(Base):
    """Human-readable, append-only audit trail -- distinct from OutboxEvent below,
    which exists purely to guarantee message delivery to Kafka."""
    __tablename__ = "order_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String, ForeignKey("orders.id"), nullable=False)
    event_type = Column(String, nullable=False)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    order = relationship("Order", back_populates="events")


class OutboxEvent(Base):
    """Transactional outbox: written atomically with the order state change it
    describes, then relayed to Kafka by a separate poller (app/consumer.py)."""
    __tablename__ = "outbox_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    topic = Column(String, nullable=False)
    key = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    payload = Column(Text, nullable=False)
    published = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
