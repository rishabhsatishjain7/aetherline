import json
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from . import models, schemas


def create_product(db: Session, product: schemas.ProductCreate) -> models.Product:
    db_product = models.Product(
        sku=product.sku,
        name=product.name,
        quantity_available=product.quantity_available,
        quantity_reserved=0,
    )
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    return db_product


def get_product(db: Session, sku: str) -> models.Product | None:
    return db.query(models.Product).filter(models.Product.sku == sku).first()


def reserve_stock(db: Session, req: schemas.ReservationRequest) -> schemas.ReservationResult:
    """
    Reserve stock for an order. Uses SELECT ... FOR UPDATE to lock the row
    for the duration of the transaction, so two concurrent reservations for
    the same SKU can't both read the same quantity_available and both succeed
    (the classic race condition in inventory systems).
    """
    product = (
        db.query(models.Product)
        .filter(models.Product.sku == req.sku)
        .with_for_update()
        .first()
    )

    if product is None:
        return schemas.ReservationResult(
            sku=req.sku, reserved=False, quantity_available=0, reason="sku_not_found"
        )

    if product.quantity_available < req.quantity:
        db.rollback()
        return schemas.ReservationResult(
            sku=req.sku,
            reserved=False,
            quantity_available=product.quantity_available,
            reason="insufficient_stock",
        )

    product.quantity_available -= req.quantity
    product.quantity_reserved += req.quantity
    db.commit()
    db.refresh(product)

    return schemas.ReservationResult(
        sku=req.sku, reserved=True, quantity_available=product.quantity_available
    )


def release_stock(db: Session, req: schemas.ReservationRequest) -> schemas.ReservationResult:
    """Release a previously reserved quantity back to available (e.g. on order cancel)."""
    product = (
        db.query(models.Product)
        .filter(models.Product.sku == req.sku)
        .with_for_update()
        .first()
    )

    if product is None:
        return schemas.ReservationResult(
            sku=req.sku, reserved=False, quantity_available=0, reason="sku_not_found"
        )

    release_qty = min(req.quantity, product.quantity_reserved)
    product.quantity_reserved -= release_qty
    product.quantity_available += release_qty
    db.commit()
    db.refresh(product)

    return schemas.ReservationResult(
        sku=req.sku, reserved=True, quantity_available=product.quantity_available
    )


# ---------------------------------------------------------------------------
# Order-level operations used by the Kafka consumer (Week 2). These wrap the
# single-SKU functions above with per-order idempotency, since Kafka delivers
# 'orders.placed' / 'orders.cancelled' at-least-once.
# ---------------------------------------------------------------------------

def enqueue_outbox_event(db: Session, topic: str, key: str, event_type: str, payload: dict) -> None:
    db.add(models.OutboxEvent(
        topic=topic, key=key, event_type=event_type, payload=json.dumps(payload),
    ))


def order_already_processed(db: Session, order_id: str) -> bool:
    """Used only for the informational/outcome lookup after a claim conflict
    below -- NOT relied on as the concurrency guard itself (see reserve_stock_for_order)."""
    return db.query(models.Reservation).filter(models.Reservation.order_id == order_id).first() is not None


def reserve_stock_for_order(db: Session, order_id: str, items: list[dict]) -> tuple[bool, str | None]:
    """
    Reserves every item for an order. On any failure, releases everything
    already reserved for this order in this call (compensating action) and
    records every attempt in the Reservation table.

    Concurrency: before touching any stock, this claims the order by
    inserting into ProcessedOrderClaim (order_id is its primary key) and
    committing immediately. If two consumer replicas race on the same
    redelivered message, only one insert can succeed -- the other hits
    IntegrityError and backs off instead of both reserving stock. This is
    what makes the operation safe under horizontal scaling; the informational
    order_already_processed() check is not sufficient on its own, because
    check-then-act has a race window between the SELECT and the reserve.

    Returns (success, failure_reason). A reason of "concurrent_processing"
    means another replica currently holds the claim and hasn't finished yet
    (as opposed to "already_processed_as_failed", which means a prior
    attempt fully completed and failed) -- the caller's retry/DLQ logic will
    naturally retry a "concurrent_processing" result on redelivery.
    """
    try:
        db.add(models.ProcessedOrderClaim(order_id=order_id))
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(models.Reservation).filter(models.Reservation.order_id == order_id).all()
        if not existing:
            # Claim exists but no outcome recorded yet -- another replica is
            # still working on it (or crashed mid-way; see the docstring on
            # ProcessedOrderClaim for the known limitation there).
            return False, "concurrent_processing"
        succeeded = all(r.status == "RESERVED" for r in existing)
        return succeeded, None if succeeded else "already_processed_as_failed"

    reserved_skus: list[dict] = []
    for item in items:
        result = reserve_stock(
            db, schemas.ReservationRequest(order_id=order_id, sku=item["sku"], quantity=item["quantity"])
        )
        if not result.reserved:
            for done in reserved_skus:
                release_stock(
                    db, schemas.ReservationRequest(order_id=order_id, sku=done["sku"], quantity=done["quantity"])
                )
                db.add(models.Reservation(
                    order_id=order_id, sku=done["sku"], quantity=done["quantity"], status="RELEASED",
                ))
            db.add(models.Reservation(
                order_id=order_id, sku=item["sku"], quantity=item["quantity"], status="FAILED",
            ))
            db.commit()
            return False, result.reason

        reserved_skus.append(item)
        db.add(models.Reservation(
            order_id=order_id, sku=item["sku"], quantity=item["quantity"], status="RESERVED",
        ))

    db.commit()
    return True, None


def release_stock_for_order(db: Session, order_id: str, items: list[dict]) -> None:
    """Idempotent: only releases reservations that are currently RESERVED for this order."""
    reservations = (
        db.query(models.Reservation)
        .filter(models.Reservation.order_id == order_id, models.Reservation.status == "RESERVED")
        .all()
    )
    if not reservations:
        return  # already released, or was never successfully reserved -- nothing to do

    for r in reservations:
        release_stock(db, schemas.ReservationRequest(order_id=order_id, sku=r.sku, quantity=r.quantity))
        r.status = "RELEASED"
    db.commit()
