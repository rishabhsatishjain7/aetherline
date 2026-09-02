"""
Thin Kafka helper. Deliberately duplicated per service rather than shared as
a library -- each microservice should be independently deployable and
versionable, and this file is small enough that duplication costs less than
a shared-package release process would.

Delivery semantics: at-least-once. Consumers auto-commit offsets after a
message is handled (successfully or DLQ'd), so a crash between "handled" and
"commit" can redeliver a message. Every consumer's handler is written to be
idempotent (checked against its own DB state) so redelivery is safe.
"""
import os
import json
import logging
import time

from kafka import KafkaProducer, KafkaConsumer
from kafka.errors import KafkaError

logger = logging.getLogger(__name__)

BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
MAX_HANDLER_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0


def get_producer(retries: int = 10, delay: float = 3.0) -> KafkaProducer:
    """Kafka may still be starting when a service container boots; retry the connection."""
    last_err = None
    for attempt in range(retries):
        try:
            return KafkaProducer(
                bootstrap_servers=BOOTSTRAP_SERVERS,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                acks="all",
            )
        except KafkaError as exc:
            last_err = exc
            logger.warning(
                "Kafka not available yet (attempt %d/%d), retrying...", attempt + 1, retries)
            time.sleep(delay)
    raise last_err


def publish_event(producer: KafkaProducer, topic: str, key: str, event_type: str, payload: dict) -> None:
    message = {"event_type": event_type, "payload": payload}
    producer.send(topic, key=key, value=message)
    producer.flush()


def get_consumer(topics: list[str], group_id: str, retries: int = 10, delay: float = 3.0) -> KafkaConsumer:
    last_err = None
    for attempt in range(retries):
        try:
            return KafkaConsumer(
                *topics,
                bootstrap_servers=BOOTSTRAP_SERVERS,
                group_id=group_id,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                key_deserializer=lambda k: k.decode("utf-8") if k else None,
                auto_offset_reset="earliest",
                enable_auto_commit=False,  # committed manually in run_consumer_loop, see below
            )
        except KafkaError as exc:
            last_err = exc
            logger.warning(
                "Kafka not available yet (attempt %d/%d), retrying...", attempt + 1, retries)
            time.sleep(delay)
    raise last_err


def run_consumer_loop(consumer: KafkaConsumer, producer: KafkaProducer, handler) -> None:
    """
    handler(topic: str, event_type: str, payload: dict) -> None
    On repeated failure for a message, the raw message is sent to
    '<topic>.dlq' instead of blocking the partition forever on a poison pill.

    Offsets are committed manually, AFTER a message is fully handled
    (successfully or DLQ'd) -- not on Kafka's auto-commit timer. With
    auto-commit, a commit interval can elapse mid-retry and advance the
    offset before the message is durably handled, which would silently
    drop it instead of the at-least-once behaviour the rest of this system
    (idempotent consumers, outbox pattern) is built to assume.
    """
    for message in consumer:
        event_type = message.value.get("event_type", "UNKNOWN")
        payload = message.value.get("payload", {})

        for attempt in range(1, MAX_HANDLER_RETRIES + 1):
            try:
                handler(message.topic, event_type, payload)
                break
            except Exception as exc:  # noqa: BLE001 - a handler bug shouldn't kill the consumer
                logger.error(
                    "Error handling %s/%s (attempt %d/%d): %s",
                    message.topic, event_type, attempt, MAX_HANDLER_RETRIES, exc,
                )
                if attempt == MAX_HANDLER_RETRIES:
                    publish_event(
                        producer,
                        f"{message.topic}.dlq",
                        message.key,
                        event_type,
                        {"original_payload": payload, "error": str(exc)},
                    )
                else:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        consumer.commit()
