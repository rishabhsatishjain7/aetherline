"""
Proves payment processing is safe when two consumer replicas (payment-service
runs 2 in k8s) both handle a redelivered 'inventory.reserved' message for the
same order_id at roughly the same time.

This is exactly the scenario a Kafka consumer group rebalance can produce
under at-least-once delivery: both replicas see the message before either
has committed its result.
"""
import threading
from unittest.mock import patch
from app import crud, models
from app.database import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import tempfile
import os


def test_concurrent_payment_processing_for_same_order_is_safe():
    """
    Uses its own file-based SQLite DB (not the `db_session` fixture) because
    this test needs two independent sessions/connections racing each other,
    which is the whole point.
    """
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

    errors = []

    def worker():
        db = SessionLocal()
        try:
            with patch("app.crud.random.random", return_value=0.99):
                crud.process_payment(db, "race-order")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"process_payment raised under concurrency: {errors}"

    db = SessionLocal()
    try:
        payments = db.query(models.Payment).filter(models.Payment.order_id == "race-order").all()
        assert len(payments) == 1, "exactly one Payment row should exist, not one per racing replica"

        outbox = db.query(models.OutboxEvent).filter(models.OutboxEvent.key == "race-order").all()
        assert len(outbox) == 1, "exactly one outbox event should be enqueued, not one per racing replica"
    finally:
        db.close()

    engine.dispose()
    os.remove(db_path)
