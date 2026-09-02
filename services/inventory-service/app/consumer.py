"""
Inventory Service Kafka worker. Subscribes to order lifecycle events and
turns them into stock reservations/releases, publishing the outcome as
events of its own via the transactional outbox.

Run as its own container (see docker-compose.yml: inventory-consumer).
"""
import json
import logging
import threading
import time

from . import crud, models
from .database import SessionLocal, engine
from .kafka_utils import get_producer, get_consumer, run_consumer_loop, publish_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ensure tables exist even if this consumer starts before (or
# independently of, as in Kubernetes) the API container that would
# otherwise create them. create_all() only creates missing tables, so
# it's safe to call from both processes.
models.Base.metadata.create_all(bind=engine)

CONSUMED_TOPICS = ["orders.placed", "orders.cancelled"]
GROUP_ID = "inventory-service"


def handle_message(topic: str, event_type: str, payload: dict) -> None:
    order_id = payload["order_id"]
    items = payload["items"]
    db = SessionLocal()
    try:
        if topic == "orders.placed":
            success, reason = crud.reserve_stock_for_order(db, order_id, items)
            if success:
                crud.enqueue_outbox_event(
                    db, "inventory.reserved", order_id, "INVENTORY_RESERVED",
                    {"order_id": order_id, "items": items},
                )
            else:
                crud.enqueue_outbox_event(
                    db, "inventory.failed", order_id, "INVENTORY_FAILED",
                    {"order_id": order_id, "items": items, "reason": reason},
                )
            db.commit()

        elif topic == "orders.cancelled":
            crud.release_stock_for_order(db, order_id, items)
            crud.enqueue_outbox_event(
                db, "inventory.released", order_id, "INVENTORY_RELEASED",
                {"order_id": order_id, "items": items},
            )
            db.commit()
        else:
            logger.warning("Unhandled topic %s", topic)
    finally:
        db.close()


def relay_outbox(poll_interval: float = 1.0) -> None:
    """Publishes unpublished outbox rows to Kafka, then marks them published."""
    producer = get_producer()
    while True:
        db = SessionLocal()
        try:
            pending = (
                db.query(models.OutboxEvent)
                .filter(models.OutboxEvent.published == 0)
                .order_by(models.OutboxEvent.id)
                .limit(50)
                .all()
            )
            for event in pending:
                publish_event(producer, event.topic, event.key, event.event_type, json.loads(event.payload))
                event.published = 1
            if pending:
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Outbox relay failed, will retry next tick")
        finally:
            db.close()
        time.sleep(poll_interval)


def main() -> None:
    relay_thread = threading.Thread(target=relay_outbox, daemon=True)
    relay_thread.start()

    producer = get_producer()
    consumer = get_consumer(CONSUMED_TOPICS, GROUP_ID)
    logger.info("Inventory consumer listening on %s", CONSUMED_TOPICS)
    run_consumer_loop(consumer, producer, handle_message)


if __name__ == "__main__":
    main()
