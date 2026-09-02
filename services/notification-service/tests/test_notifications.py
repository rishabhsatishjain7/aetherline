from app import crud, models


def test_send_notification_records_it(db_session):
    crud.send_notification(db_session, "order-1", "ORDER_CONFIRMED")
    notifications = crud.get_notifications(db_session, "order-1")
    assert len(notifications) == 1
    assert notifications[0].notification_type == "ORDER_CONFIRMED"


def test_redelivered_event_does_not_duplicate_notification(db_session):
    """The unique constraint on (order_id, notification_type) is the
    idempotency guard -- a redelivered Kafka message for an order that's
    already been notified must not record (or 'send') a second one."""
    crud.send_notification(db_session, "order-1", "ORDER_CONFIRMED")
    crud.send_notification(db_session, "order-1", "ORDER_CONFIRMED")  # redelivery

    notifications = crud.get_notifications(db_session, "order-1")
    assert len(notifications) == 1


def test_different_notification_types_for_same_order_both_recorded(db_session):
    """Distinct types (e.g. a cancellation confirmation after a prior
    failure notice) are legitimately separate rows, not deduped against
    each other."""
    crud.send_notification(db_session, "order-1", "ORDER_FAILED")
    crud.send_notification(db_session, "order-1", "ORDER_CANCELLATION_CONFIRMED")

    notifications = crud.get_notifications(db_session, "order-1")
    assert len(notifications) == 2


def test_get_notifications_endpoint(client, db_session):
    crud.send_notification(db_session, "order-2", "ORDER_CONFIRMED")
    resp = client.get("/notifications/order-2")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["notification_type"] == "ORDER_CONFIRMED"


def test_get_notifications_empty_for_unknown_order(client):
    resp = client.get("/notifications/does-not-exist")
    assert resp.status_code == 200
    assert resp.json() == []
