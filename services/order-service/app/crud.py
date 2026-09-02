import json
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from . import models, schemas


def get_order(db: Session, order_id: str) -> models.Order | None:
    return db.query(models.Order).filter(models.Order.id == order_id).first()


def get_order_for_update(db: Session, order_id: str) -> models.Order | None:
    """Row-locked fetch, used by the consumer-side apply_* functions below so
    two replicas processing a redelivered message for the same order can't
    both read PENDING and both apply a transition concurrently."""
    return db.query(models.Order).filter(models.Order.id == order_id).with_for_update().first()


def get_order_by_idempotency_key(db: Session, key: str) -> models.Order | None:
    return db.query(models.Order).filter(models.Order.idempotency_key == key).first()


def _log_event(db: Session, order_id: str, event_type: str, payload: dict | None = None) -> None:
    db.add(models.OrderEvent(
        order_id=order_id, event_type=event_type,
        payload=json.dumps(payload) if payload is not None else None,
    ))


def _enqueue_outbox(db: Session, topic: str, key: str, event_type: str, payload: dict) -> None:
    db.add(models.OutboxEvent(topic=topic, key=key, event_type=event_type, payload=json.dumps(payload)))


def _items_payload(order: models.Order) -> list[dict]:
    return [{"sku": i.sku, "quantity": i.quantity} for i in order.items]


def create_order(
    db: Session, order_in: schemas.OrderCreate, idempotency_key: str | None = None
) -> models.Order:
    """
    Creates the order as PENDING and atomically enqueues an ORDER_PLACED
    outbox event (published to Kafka's 'orders.placed' topic by the relayer).
    This function no longer knows the outcome -- reservation and payment now
    happen asynchronously, resolved by consumer.py, which moves the order to
    CONFIRMED or FAILED. Callers get a PENDING order back immediately (202).

    Idempotency: a retried request with the same key returns the existing
    order untouched. The initial lookup below is an optimization for the
    common case, not the concurrency guard -- two requests with the same key
    arriving at different api replicas at the same instant can both pass it
    before either commits. What actually prevents a duplicate order is
    Order.idempotency_key being a unique column: the loser's commit raises
    IntegrityError, caught below, and re-fetches the winner's row instead of
    erroring out to the client.
    """
    if idempotency_key:
        existing = get_order_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return existing

    order = models.Order(status=models.OrderStatus.PENDING, idempotency_key=idempotency_key)
    for item in order_in.items:
        order.items.append(models.OrderItem(sku=item.sku, quantity=item.quantity))

    db.add(order)
    db.flush()  # assigns order.id
    _log_event(db, order.id, "ORDER_CREATED")
    _enqueue_outbox(
        db, "orders.placed", order.id, "ORDER_PLACED",
        {"order_id": order.id, "items": _items_payload(order)},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Lost the race to another replica creating an order with the same
        # idempotency_key -- return the winner's order instead of failing.
        winner = get_order_by_idempotency_key(db, idempotency_key) if idempotency_key else None
        if winner is None:
            raise  # not an idempotency-key conflict -- a real error, surface it
        return winner

    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: str) -> tuple[models.Order | None, str | None]:
    """
    Optimistic cancellation: the order is marked CANCELLED immediately and an
    ORDER_CANCELLED event is enqueued so Inventory releases any reserved
    stock asynchronously. Trade-off: for a brief window the order shows
    CANCELLED while stock release is still in flight -- acceptable here since
    nothing reads "is this SKU free" directly off order status.

    Takes order_id (not an already-fetched Order) and does the fetch itself
    via get_order_for_update(), so the "is it already cancelled" check and
    the cancellation happen under the same row lock. Two concurrent cancel
    requests for the same order previously raced here the same way the
    apply_* functions did above: both could fetch the order, both see it's
    not yet CANCELLED, and both proceed -- duplicating the audit log entry
    and the outbox event (harmless in effect, since inventory-service's
    release is itself idempotent, but still incorrect bookkeeping).

    Returns (order, error): error is "order_not_found" or
    "order_already_cancelled" when no cancellation was performed, else None.
    """
    order = get_order_for_update(db, order_id)
    if order is None:
        return None, "order_not_found"
    if order.status == models.OrderStatus.CANCELLED:
        return order, "order_already_cancelled"

    _enqueue_outbox(
        db, "orders.cancelled", order.id, "ORDER_CANCELLED",
        {"order_id": order.id, "items": _items_payload(order)},
    )
    order.status = models.OrderStatus.CANCELLED
    _log_event(db, order.id, "ORDER_CANCELLED")
    db.commit()
    db.refresh(order)
    return order, None


# ---------------------------------------------------------------------------
# Called by the Kafka consumer (app/consumer.py) as inventory/payment results
# arrive. Every function here fetches the order with get_order_for_update()
# (SELECT ... FOR UPDATE) rather than the plain get_order() -- two
# order-consumer replicas can receive the same redelivered message and both
# reach the "order.status != PENDING" check at the same instant; the row
# lock serializes them so only one actually applies the transition instead
# of both committing (which would double-log audit events and double-enqueue
# outbox events downstream).
# ---------------------------------------------------------------------------

def apply_inventory_failed(db: Session, order_id: str, reason: str) -> None:
    order = get_order_for_update(db, order_id)
    if order is None or order.status != models.OrderStatus.PENDING:
        return
    order.status = models.OrderStatus.FAILED
    _log_event(db, order_id, "RESERVATION_FAILED", {"reason": reason})
    _enqueue_outbox(db, "orders.failed", order_id, "ORDER_FAILED", {"order_id": order_id, "reason": reason})
    db.commit()


def apply_inventory_reserved(db: Session, order_id: str) -> None:
    """Inventory is secured; this just logs the milestone. Payment is triggered
    downstream by payment-service, which listens on 'inventory.reserved' directly."""
    order = get_order_for_update(db, order_id)
    if order is None or order.status != models.OrderStatus.PENDING:
        return
    _log_event(db, order_id, "INVENTORY_RESERVED")
    db.commit()


def apply_payment_succeeded(db: Session, order_id: str) -> None:
    order = get_order_for_update(db, order_id)
    if order is None or order.status != models.OrderStatus.PENDING:
        return
    order.status = models.OrderStatus.CONFIRMED
    _log_event(db, order_id, "ORDER_CONFIRMED")
    _enqueue_outbox(db, "orders.confirmed", order_id, "ORDER_CONFIRMED", {"order_id": order_id})
    db.commit()


def apply_payment_failed(db: Session, order_id: str, reason: str) -> None:
    """
    Payment failed after inventory was already reserved -- this is the
    compensating path: release the stock (via the same 'orders.cancelled'
    topic Inventory already knows how to handle) and fail the order.
    """
    order = get_order_for_update(db, order_id)
    if order is None or order.status != models.OrderStatus.PENDING:
        return
    order.status = models.OrderStatus.FAILED
    _log_event(db, order_id, "PAYMENT_FAILED", {"reason": reason})
    _enqueue_outbox(
        db, "orders.cancelled", order_id, "ORDER_CANCELLED",
        {"order_id": order_id, "items": _items_payload(order)},
    )
    _enqueue_outbox(db, "orders.failed", order_id, "ORDER_FAILED", {"order_id": order_id, "reason": reason})
    db.commit()
