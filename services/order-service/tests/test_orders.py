"""
Order service tests for the Week 2 event-driven flow.

There's no real Kafka broker in the test environment, so these tests split
into two kinds:
  - API-level: POST/GET/cancel behave correctly and the order lands as PENDING.
  - Consumer-level: call the same crud functions app/consumer.py calls when a
    Kafka message arrives (apply_inventory_reserved, apply_payment_failed,
    etc.) directly against a db session, simulating message delivery without
    needing a broker. This is the same thing you'd do with a real broker in
    an integration test -- unit-test the handler logic against a fake message.
"""
from app import crud, models


def _create_order(client, items=None, idempotency_key=None):
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return client.post(
        "/orders",
        json={"items": items or [{"sku": "WIDGET-1", "quantity": 2}]},
        headers=headers,
    )


def test_create_order_returns_202_pending(client):
    resp = _create_order(client)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["items"][0]["sku"] == "WIDGET-1"


def test_create_order_enqueues_outbox_event(client, db_session):
    resp = _create_order(client)
    order_id = resp.json()["id"]

    outbox = db_session.query(models.OutboxEvent).filter(
        models.OutboxEvent.key == order_id
    ).all()
    assert len(outbox) == 1
    assert outbox[0].topic == "orders.placed"
    assert outbox[0].event_type == "ORDER_PLACED"
    assert outbox[0].published == 0


def test_get_order_not_found(client):
    resp = client.get("/orders/does-not-exist")
    assert resp.status_code == 404


def test_idempotency_key_does_not_create_a_second_order(client, db_session):
    resp1 = _create_order(client, idempotency_key="key-abc")
    resp2 = _create_order(client, idempotency_key="key-abc")

    assert resp1.json()["id"] == resp2.json()["id"]
    count = db_session.query(models.Order).count()
    assert count == 1

    outbox = db_session.query(models.OutboxEvent).filter(models.OutboxEvent.topic == "orders.placed").all()
    assert len(outbox) == 1  # only placed once, not re-placed on the retry


def test_full_success_lifecycle_via_consumer_handlers(client, db_session):
    """Simulates: order placed -> inventory reserves -> payment succeeds."""
    order_id = _create_order(client).json()["id"]

    crud.apply_inventory_reserved(db_session, order_id)
    crud.apply_payment_succeeded(db_session, order_id)

    resp = client.get(f"/orders/{order_id}")
    assert resp.json()["status"] == "CONFIRMED"

    events = client.get(f"/orders/{order_id}/events").json()
    assert [e["event_type"] for e in events] == [
        "ORDER_CREATED", "INVENTORY_RESERVED", "ORDER_CONFIRMED",
    ]

    confirmed_outbox = db_session.query(models.OutboxEvent).filter(
        models.OutboxEvent.topic == "orders.confirmed"
    ).all()
    assert len(confirmed_outbox) == 1


def test_inventory_failure_marks_order_failed(client, db_session):
    order_id = _create_order(client).json()["id"]

    crud.apply_inventory_failed(db_session, order_id, "insufficient_stock")

    resp = client.get(f"/orders/{order_id}")
    assert resp.json()["status"] == "FAILED"

    events = client.get(f"/orders/{order_id}/events").json()
    assert events[-1]["event_type"] == "RESERVATION_FAILED"

    failed_outbox = db_session.query(models.OutboxEvent).filter(
        models.OutboxEvent.topic == "orders.failed"
    ).all()
    assert len(failed_outbox) == 1


def test_payment_failure_triggers_compensating_release_and_fails_order(client, db_session):
    """Inventory succeeded, but payment failed afterwards -- order should end
    up FAILED, and an orders.cancelled event should be enqueued so Inventory
    releases the stock it already reserved."""
    order_id = _create_order(client).json()["id"]

    crud.apply_inventory_reserved(db_session, order_id)
    crud.apply_payment_failed(db_session, order_id, "mock_payment_declined")

    resp = client.get(f"/orders/{order_id}")
    assert resp.json()["status"] == "FAILED"

    cancelled_outbox = db_session.query(models.OutboxEvent).filter(
        models.OutboxEvent.topic == "orders.cancelled"
    ).all()
    assert len(cancelled_outbox) == 1

    failed_outbox = db_session.query(models.OutboxEvent).filter(
        models.OutboxEvent.topic == "orders.failed"
    ).all()
    assert len(failed_outbox) == 1


def test_redelivered_message_is_a_no_op_once_order_resolved(client, db_session):
    """If Kafka redelivers 'payments.succeeded' after the order is already
    CONFIRMED (at-least-once delivery), applying it again must not re-fire
    a second ORDER_CONFIRMED event or outbox entry."""
    order_id = _create_order(client).json()["id"]
    crud.apply_inventory_reserved(db_session, order_id)
    crud.apply_payment_succeeded(db_session, order_id)

    crud.apply_payment_succeeded(db_session, order_id)  # redelivery

    confirmed_outbox = db_session.query(models.OutboxEvent).filter(
        models.OutboxEvent.topic == "orders.confirmed"
    ).all()
    assert len(confirmed_outbox) == 1  # still just one, not two


def test_cancel_order_enqueues_cancellation_event(client, db_session):
    order_id = _create_order(client).json()["id"]

    resp = client.post(f"/orders/{order_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"

    cancelled_outbox = db_session.query(models.OutboxEvent).filter(
        models.OutboxEvent.topic == "orders.cancelled"
    ).all()
    assert len(cancelled_outbox) == 1


def test_cancel_already_cancelled_order_conflicts(client):
    order_id = _create_order(client).json()["id"]
    client.post(f"/orders/{order_id}/cancel")
    resp = client.post(f"/orders/{order_id}/cancel")
    assert resp.status_code == 409
