"""Proves the unique-constraint + IntegrityError-catch pattern in
crud.send_notification is actually safe under real concurrent access, not
just in the single-threaded happy path the other tests exercise."""
import os
import tempfile
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models
from app.database import Base


def test_concurrent_notifications_for_same_order_type_dedupe():
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
            crud.send_notification(db, "race-order", "ORDER_CONFIRMED")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"send_notification raised under concurrency: {errors}"

    db = SessionLocal()
    try:
        rows = db.query(models.Notification).filter(models.Notification.order_id == "race-order").all()
        assert len(rows) == 1
    finally:
        db.close()

    engine.dispose()
    os.remove(db_path)
