"""
Order-service concurrency tests. Uses its own file-based SQLite DB (not the
`client`/`db_session` fixtures) because these tests need two independent
threads/sessions racing each other against the same underlying data, which
is the whole point.
"""
import os
import tempfile
import threading

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.database import Base, get_db
from app import crud, models, schemas


def _fresh_db():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    # SQLite doesn't support real row-level locking: with_for_update() compiles
    # to a no-op on this dialect, and by default SQLite's transactions are
    # "deferred" -- a bare SELECT doesn't take any lock at all, so two threads
    # can both read stale data before either writes. Forcing every transaction
    # to open with BEGIN IMMEDIATE (SQLAlchemy's documented recipe for this)
    # makes SQLite acquire a write lock at the START of the transaction instead
    # of at the first write, which is what actually makes the concurrency
    # tests below meaningful instead of silently flaky.
    from sqlalchemy import event as _sa_event

    @_sa_event.listens_for(engine, "connect")
    def _sqlite_set_isolation(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @_sa_event.listens_for(engine, "begin")
    def _sqlite_begin_immediate(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine, SessionLocal, db_path


def test_concurrent_requests_with_same_idempotency_key_create_only_one_order():
    """Two HTTP requests hitting different api replicas at the same instant
    with the same Idempotency-Key -- only one Order row should ever exist."""
    engine, SessionLocal, db_path = _fresh_db()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    results = []
    errors = []

    def place_order():
        try:
            resp = client.post(
                "/orders",
                json={"items": [{"sku": "WIDGET-1", "quantity": 1}]},
                headers={"Idempotency-Key": "race-key"},
            )
            results.append(resp)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=place_order) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    app.dependency_overrides.clear()

    assert not errors, f"POST /orders raised under concurrency: {errors}"
    assert all(r.status_code == 202 for r in results)
    order_ids = {r.json()["id"] for r in results}
    assert len(order_ids) == 1, f"expected both requests to resolve to one order, got {order_ids}"

    db = SessionLocal()
    try:
        count = db.query(models.Order).count()
        assert count == 1
    finally:
        db.close()

    engine.dispose()
    os.remove(db_path)


def test_concurrent_payment_succeeded_events_confirm_order_exactly_once():
    """Simulates two order-consumer replicas both handling a redelivered
    'payments.succeeded' message for the same order -- the order should end
    up CONFIRMED with exactly one ORDER_CONFIRMED audit entry and exactly
    one 'orders.confirmed' outbox event, not two of each."""
    engine, SessionLocal, db_path = _fresh_db()

    seed_db = SessionLocal()
    order, _ = None, None
    created = crud.create_order(
        seed_db, schemas.OrderCreate(items=[schemas.OrderItemIn(sku="WIDGET-1", quantity=1)])
    )
    order_id = created.id
    crud.apply_inventory_reserved(seed_db, order_id)
    seed_db.close()

    errors = []

    def worker():
        db = SessionLocal()
        try:
            crud.apply_payment_succeeded(db, order_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"apply_payment_succeeded raised under concurrency: {errors}"

    check_db = SessionLocal()
    try:
        order = crud.get_order(check_db, order_id)
        assert order.status == models.OrderStatus.CONFIRMED

        confirmed_events = check_db.query(models.OrderEvent).filter(
            models.OrderEvent.order_id == order_id, models.OrderEvent.event_type == "ORDER_CONFIRMED"
        ).all()
        assert len(confirmed_events) == 1

        confirmed_outbox = check_db.query(models.OutboxEvent).filter(
            models.OutboxEvent.topic == "orders.confirmed", models.OutboxEvent.key == order_id
        ).all()
        assert len(confirmed_outbox) == 1
    finally:
        check_db.close()

    engine.dispose()
    os.remove(db_path)


def test_concurrent_cancel_requests_for_same_order_cancel_exactly_once():
    """Two clients hitting POST /orders/{id}/cancel at the same instant --
    exactly one should succeed with 200, the other should see 409
    order_already_cancelled, and only one ORDER_CANCELLED audit entry /
    outbox event should exist (not one per request)."""
    engine, SessionLocal, db_path = _fresh_db()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    create_resp = client.post("/orders", json={"items": [{"sku": "WIDGET-1", "quantity": 1}]})
    order_id = create_resp.json()["id"]

    results = []
    errors = []

    def cancel():
        try:
            results.append(client.post(f"/orders/{order_id}/cancel"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=cancel) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    app.dependency_overrides.clear()

    assert not errors, f"cancel raised under concurrency: {errors}"
    status_codes = sorted(r.status_code for r in results)
    assert status_codes == [200, 409], f"expected exactly one 200 and one 409, got {status_codes}"

    db = SessionLocal()
    try:
        cancelled_events = db.query(models.OrderEvent).filter(
            models.OrderEvent.order_id == order_id, models.OrderEvent.event_type == "ORDER_CANCELLED"
        ).all()
        assert len(cancelled_events) == 1

        cancelled_outbox = db.query(models.OutboxEvent).filter(
            models.OutboxEvent.topic == "orders.cancelled", models.OutboxEvent.key == order_id
        ).all()
        assert len(cancelled_outbox) == 1
    finally:
        db.close()

    engine.dispose()
    os.remove(db_path)
