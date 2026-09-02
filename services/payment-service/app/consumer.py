"""Payment Service Kafka worker: consumes 'inventory.reserved' (stock is
secured, safe to charge) and publishes payments.succeeded/payments.failed."""
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

CONSUMED_TOPICS = ["inventory.reserved"]
GROUP_ID = "payment-service"


def handle_message(topic: str, event_type: str, payload: dict) -> None:
    db = SessionLocal()
    try:
        crud.process_payment(db, payload["order_id"])
    finally:
        db.close()


def relay_outbox(poll_interval: float = 1.0) -> None:
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
    threading.Thread(target=relay_outbox, daemon=True).start()
    producer = get_producer()
    consumer = get_consumer(CONSUMED_TOPICS, GROUP_ID)
    logger.info("Payment consumer listening on %s", CONSUMED_TOPICS)
    run_consumer_loop(consumer, producer, handle_message)


if __name__ == "__main__":
    main()
