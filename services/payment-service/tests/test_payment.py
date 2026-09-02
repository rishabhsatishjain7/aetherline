import os
from unittest.mock import patch
from app import crud, models


def test_process_payment_success_records_payment_and_outbox(db_session):
    with patch("app.crud.random.random", return_value=0.99):  # >= failure rate 0.0 -> succeeds
        crud.process_payment(db_session, "order-1")

    payment = crud.get_payment(db_session, "order-1")
    assert payment.status == "SUCCEEDED"

    outbox = db_session.query(models.OutboxEvent).filter(models.OutboxEvent.key == "order-1").all()
    assert len(outbox) == 1
    assert outbox[0].topic == "payments.succeeded"


def test_process_payment_failure_records_failure_and_outbox(db_session, monkeypatch):
    monkeypatch.setenv("PAYMENT_FAILURE_RATE", "1.0")
    with patch("app.crud.PAYMENT_FAILURE_RATE", 1.0):
        crud.process_payment(db_session, "order-2")

    payment = crud.get_payment(db_session, "order-2")
    assert payment.status == "FAILED"

    outbox = db_session.query(models.OutboxEvent).filter(models.OutboxEvent.key == "order-2").all()
    assert outbox[0].topic == "payments.failed"


def test_redelivered_message_does_not_reprocess(db_session):
    with patch("app.crud.random.random", return_value=0.99):
        crud.process_payment(db_session, "order-3")
        crud.process_payment(db_session, "order-3")  # redelivery

    outbox = db_session.query(models.OutboxEvent).filter(models.OutboxEvent.key == "order-3").all()
    assert len(outbox) == 1  # not processed twice


def test_get_payment_endpoint_not_found(client):
    resp = client.get("/payments/does-not-exist")
    assert resp.status_code == 404


def test_get_payment_endpoint_found(client, db_session):
    with patch("app.crud.random.random", return_value=0.99):
        crud.process_payment(db_session, "order-4")
    resp = client.get("/payments/order-4")
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCEEDED"
