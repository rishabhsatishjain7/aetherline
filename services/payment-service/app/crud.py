import json
import os
import random
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from . import models

# Simulated failure rate for the mock payment processor, configurable so the
# failure path (and the order-service compensating release it triggers) can
# be exercised on demand without touching real payment rails.
PAYMENT_FAILURE_RATE = float(os.getenv("PAYMENT_FAILURE_RATE", "0.0"))


def get_payment(db: Session, order_id: str) -> models.Payment | None:
    return db.query(models.Payment).filter(models.Payment.order_id == order_id).first()


def process_payment(db: Session, order_id: str) -> None:
    """
    Idempotent -- and atomically so. The initial get_payment() check below is
    just an optimization to skip work for the common case (a message that
    isn't a redelivery); it is NOT what makes this safe, because two
    concurrent calls (e.g. two payment-consumer replicas both receiving the
    same redelivered message during a Kafka rebalance) can both pass that
    check before either commits.

    What actually makes this safe is Payment.order_id being a primary key:
    only one of two concurrent inserts for the same order_id can succeed.
    The loser's commit raises IntegrityError, which is caught below and
    treated as "someone else already processed this" -- a safe no-op rather
    than a crashed consumer or a duplicate outbox event.
    """
    if get_payment(db, order_id) is not None:
        return

    succeeded = random.random() >= PAYMENT_FAILURE_RATE
    payment = models.Payment(order_id=order_id, status="SUCCEEDED" if succeeded else "FAILED")
    db.add(payment)

    topic = "payments.succeeded" if succeeded else "payments.failed"
    event_type = "PAYMENT_SUCCEEDED" if succeeded else "PAYMENT_FAILED"
    payload = {"order_id": order_id}
    if not succeeded:
        payload["reason"] = "mock_payment_declined"

    db.add(models.OutboxEvent(
        topic=topic, key=order_id, event_type=event_type, payload=json.dumps(payload),
    ))

    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # lost the race to another replica processing the same order_id -- safe no-op
