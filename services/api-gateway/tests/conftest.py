import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Both gateway credentials FAIL CLOSED: importing app.main without
# API_GATEWAY_SHARED_SECRET or JWT_SECRET_KEY raises, exactly as it would in
# a misconfigured deployment. The suite therefore configures both explicitly
# *before* importing the app -- the same thing a real deployment has to do --
# and sets them unconditionally so the suite is deterministic regardless of
# the ambient environment. The refusal-to-start behaviour itself is covered
# by the subprocess tests in test_gateway.py / test_auth.py, which import the
# app in fresh interpreters with each variable removed/blanked.
#
# GATEWAY_DATABASE_URL must also point somewhere import-time create_all()
# can reach: the default is the Docker hostname `gateway-db`, which doesn't
# resolve outside Docker. The import-time value is a harmless local fallback
# file; the actual per-test database is a separate SQLite file set up below
# via dependency_overrides (same pattern as the other services' conftests).
TEST_SHARED_SECRET = "api-gateway-test-secret"
TEST_JWT_SECRET = "api-gateway-test-jwt-secret"
os.environ["API_GATEWAY_SHARED_SECRET"] = TEST_SHARED_SECRET
os.environ["JWT_SECRET_KEY"] = TEST_JWT_SECRET
os.environ.setdefault("GATEWAY_DATABASE_URL", "sqlite:///./_test_import_fallback.db")

from app.main import app  # noqa: E402  (must run after the env vars are set)
from app import auth as gateway_auth  # noqa: E402
from app.database import Base, get_db  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    """Throttle buckets are process-global; isolate every test."""
    gateway_auth.reset_throttle_state()
    yield
    gateway_auth.reset_throttle_state()


@pytest.fixture()
def _db_and_clients():
    # File-based temp DB per test so register/login write to a clean slate.
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)

    engine = create_engine(f"sqlite:///{db_path}")
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, headers={"X-API-Key": TEST_SHARED_SECRET}) as authed:
        with TestClient(app) as anonymous:
            yield authed, anonymous, TestingSessionLocal

    app.dependency_overrides.clear()
    engine.dispose()
    os.remove(db_path)


@pytest.fixture()
def client(_db_and_clients):
    """A gateway client that authenticates the way every real caller must."""
    authed, _, _ = _db_and_clients
    return authed


@pytest.fixture()
def anonymous_client(_db_and_clients):
    """A gateway client that sends no credential header at all."""
    _, anonymous, _ = _db_and_clients
    return anonymous


@pytest.fixture()
def db_session(_db_and_clients):
    """A raw session against the same test DB the API uses."""
    _, _, session_factory = _db_and_clients
    session = session_factory()
    yield session
    session.close()

