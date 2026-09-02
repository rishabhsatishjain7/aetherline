"""
Tests for crud.reserve_stock_for_order / release_stock_for_order -- the
functions app/consumer.py calls when an 'orders.placed' / 'orders.cancelled'
Kafka message arrives. No real broker needed: these call the same functions
the consumer calls, directly, simulating message delivery.
"""
from app import crud, models


def _seed(db_session, sku, qty):
    from app.database import Base
    p = models.Product(sku=sku, name=sku, quantity_available=qty, quantity_reserved=0)
    db_session.add(p)
    db_session.commit()


def test_multi_item_order_reserves_all_items(db_session):
    _seed(db_session, "A", 5)
    _seed(db_session, "B", 5)

    success, reason = crud.reserve_stock_for_order(
        db_session, "order-1", [{"sku": "A", "quantity": 2}, {"sku": "B", "quantity": 3}]
    )
    assert success is True
    assert reason is None

    a = crud.get_product(db_session, "A")
    b = crud.get_product(db_session, "B")
    assert a.quantity_available == 3
    assert b.quantity_available == 2


def test_partial_failure_releases_already_reserved_items(db_session):
    """First item reserves fine, second doesn't have enough stock -- the
    first item's reservation should be rolled back (compensating release)."""
    _seed(db_session, "A", 5)
    _seed(db_session, "B", 1)

    success, reason = crud.reserve_stock_for_order(
        db_session, "order-1", [{"sku": "A", "quantity": 2}, {"sku": "B", "quantity": 10}]
    )
    assert success is False
    assert reason == "insufficient_stock"

    a = crud.get_product(db_session, "A")
    assert a.quantity_available == 5  # released back
    assert a.quantity_reserved == 0


def test_redelivered_orders_placed_message_is_idempotent(db_session):
    """Simulates Kafka redelivering the same 'orders.placed' message twice --
    stock must only be decremented once."""
    _seed(db_session, "A", 5)

    crud.reserve_stock_for_order(db_session, "order-1", [{"sku": "A", "quantity": 2}])
    success, reason = crud.reserve_stock_for_order(db_session, "order-1", [{"sku": "A", "quantity": 2}])

    assert success is True
    a = crud.get_product(db_session, "A")
    assert a.quantity_available == 3  # NOT 1 -- second call was a no-op


def test_release_stock_for_order_is_idempotent(db_session):
    _seed(db_session, "A", 5)
    crud.reserve_stock_for_order(db_session, "order-1", [{"sku": "A", "quantity": 2}])

    crud.release_stock_for_order(db_session, "order-1", [{"sku": "A", "quantity": 2}])
    crud.release_stock_for_order(db_session, "order-1", [{"sku": "A", "quantity": 2}])  # redelivery

    a = crud.get_product(db_session, "A")
    assert a.quantity_available == 5  # not double-released past original stock
    assert a.quantity_reserved == 0


def test_concurrent_reserve_for_same_order_only_reserves_once():
    """
    Simulates two inventory-consumer replicas both receiving a redelivered
    'orders.placed' message for the same order at roughly the same time --
    the scenario that motivated ProcessedOrderClaim. Without the atomic
    claim, both threads could pass a check-then-act idempotency check before
    either commits, and both would decrement stock.
    """
    import os
    import tempfile
    import threading
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base

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

    seed_db = SessionLocal()
    seed_db.add(models.Product(sku="A", name="A", quantity_available=5, quantity_reserved=0))
    seed_db.commit()
    seed_db.close()

    results = []
    errors = []

    def worker():
        db = SessionLocal()
        try:
            success, reason = crud.reserve_stock_for_order(db, "race-order", [{"sku": "A", "quantity": 2}])
            results.append((success, reason))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"reserve_stock_for_order raised under concurrency: {errors}"

    check_db = SessionLocal()
    try:
        product = crud.get_product(check_db, "A")
        # 5 - 2 = 3, NOT 5 - 4 = 1 -- proves the second racing call did not
        # also decrement stock for the same order.
        assert product.quantity_available == 3
        assert product.quantity_reserved == 2

        claims = check_db.query(models.ProcessedOrderClaim).filter(
            models.ProcessedOrderClaim.order_id == "race-order"
        ).all()
        assert len(claims) == 1
    finally:
        check_db.close()

    engine.dispose()
    os.remove(db_path)
