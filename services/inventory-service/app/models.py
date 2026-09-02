from sqlalchemy import Column, String, Integer, CheckConstraint, DateTime, func
from .database import Base


class Product(Base):
    __tablename__ = "products"

    sku = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    quantity_available = Column(Integer, nullable=False, default=0)
    quantity_reserved = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("quantity_available >= 0", name="ck_qty_available_nonneg"),
        CheckConstraint("quantity_reserved >= 0", name="ck_qty_reserved_nonneg"),
    )


class Reservation(Base):
    """
    One row per (order_id, sku) that inventory has ever attempted to reserve.
    Records the *outcome* per SKU, used by release_stock_for_order and to
    answer "did this order succeed or fail" after the fact. This table does
    NOT guard against concurrent double-processing on its own -- see
    ProcessedOrderClaim below for that.
    """
    __tablename__ = "reservations"

    order_id = Column(String, primary_key=True)
    sku = Column(String, primary_key=True)
    quantity = Column(Integer, nullable=False)
    status = Column(String, nullable=False)  # RESERVED | RELEASED | FAILED
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ProcessedOrderClaim(Base):
    """
    Single row per order_id, inserted (and committed) BEFORE any stock is
    touched for that order. order_id is the primary key, so if two consumer
    replicas both receive a redelivered 'orders.placed' message (a real
    scenario under Kafka's at-least-once delivery during a rebalance) and
    both attempt this insert concurrently, only one commit can succeed --
    the DB serializes it for us. The loser catches IntegrityError and treats
    the order as already being handled, instead of both replicas racing
    reserve_stock() and double-decrementing stock.

    Known limitation: if the winning replica crashes after this row commits
    but before it finishes reserving every item, the order is stuck --
    future redeliveries will see the claim and back off forever rather than
    resuming the work. A production version would add a `claimed_at`
    timestamp and let a redelivery re-claim a stale (older than N minutes,
    no matching Reservation rows) claim. Not implemented here to keep the
    happy-path locking logic legible; noted as a follow-up.
    """
    __tablename__ = "processed_order_claims"

    order_id = Column(String, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class OutboxEvent(Base):
    """Transactional outbox: written atomically with the DB state change it describes."""
    __tablename__ = "outbox_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    topic = Column(String, nullable=False)
    key = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    payload = Column(String, nullable=False)  # JSON-encoded
    published = Column(Integer, nullable=False, default=0)  # 0/1, avoids Boolean quirks across DBs
    created_at = Column(DateTime(timezone=True), server_default=func.now())
