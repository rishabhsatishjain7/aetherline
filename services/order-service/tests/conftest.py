import tempfile
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Must be set BEFORE importing app.main: main.py calls create_all() at
# import time against whatever DATABASE_URL resolves to, which defaults
# to the Docker service hostname (e.g. 'inventory-db') and doesn't
# resolve outside Docker. Point it at a harmless local file instead --
# the actual per-test database is a separate SQLite file set up below
# via dependency_overrides; this only prevents the import-time create_all()
# from trying to reach an unreachable Postgres host.
os.environ.setdefault("DATABASE_URL", "sqlite:///./_test_import_fallback.db")

from app.main import app
from app.database import Base, get_db


@pytest.fixture()
def db_and_client():
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
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c, TestingSessionLocal

    app.dependency_overrides.clear()
    engine.dispose()
    os.remove(db_path)


@pytest.fixture()
def client(db_and_client):
    c, _ = db_and_client
    return c


@pytest.fixture()
def db_session(db_and_client):
    """A raw session against the same test DB the API uses -- lets tests call
    consumer-side crud functions directly, simulating a Kafka message arriving,
    without needing a real Kafka broker in the test environment."""
    _, SessionLocal = db_and_client
    session = SessionLocal()
    yield session
    session.close()
