"""Notification Service Kafka worker. Terminal consumer -- it doesn't publish
further events, so unlike the other services it has no outbox/relayer."""
import logging

from . import crud, models
from .database import SessionLocal, engine
from .kafka_utils import get_producer, get_consumer, run_consumer_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ensure tables exist even if this consumer starts before (or
# independently of, as in Kubernetes) the API container that would
# otherwise create them. create_all() only creates missing tables, so
# it's safe to call from both processes.
models.Base.metadata.create_all(bind=engine)

CONSUMED_TOPICS = ["orders.confirmed", "orders.failed", "inventory.released"]
GROUP_ID = "notification-service"

_TYPE_MAP = {
    "orders.confirmed": "ORDER_CONFIRMED",
    "orders.failed": "ORDER_FAILED",
    "inventory.released": "ORDER_CANCELLATION_CONFIRMED",
}


def handle_message(topic: str, event_type: str, payload: dict) -> None:
    db = SessionLocal()
    try:
        crud.send_notification(db, payload["order_id"], _TYPE_MAP.get(topic, event_type))
    finally:
        db.close()


def main() -> None:
    producer = get_producer()  # only needed so failed messages can go to a .dlq topic
    consumer = get_consumer(CONSUMED_TOPICS, GROUP_ID)
    logger.info("Notification consumer listening on %s", CONSUMED_TOPICS)
    run_consumer_loop(consumer, producer, handle_message)


if __name__ == "__main__":
    main()
