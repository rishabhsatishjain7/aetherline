import os

import pytest
from fastapi.testclient import TestClient

# The gateway fails closed: importing app.main without
# API_GATEWAY_SHARED_SECRET raises, exactly as it would in a misconfigured
# deployment. The suite therefore configures a secret explicitly *before*
# importing the app -- the same thing a real deployment has to do -- and sets it
# unconditionally so the suite is deterministic regardless of the ambient
# environment. The refusal-to-start behaviour itself is covered by the
# subprocess tests in test_gateway.py, which import the app in a fresh
# interpreter with the variable removed/blanked.
TEST_SHARED_SECRET = "api-gateway-test-secret"
os.environ["API_GATEWAY_SHARED_SECRET"] = TEST_SHARED_SECRET

from app.main import app  # noqa: E402  (must run after the env var is set)


@pytest.fixture()
def client():
    """A gateway client that authenticates the way every real caller must."""
    with TestClient(app, headers={"X-API-Key": TEST_SHARED_SECRET}) as c:
        yield c


@pytest.fixture()
def anonymous_client():
    """A gateway client that sends no X-API-Key header at all."""
    with TestClient(app) as c:
        yield c

