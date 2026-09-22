"""
Tests the gateway's routing and error-translation logic by mocking the
outbound httpx client -- no real order-service/inventory-service etc. need
to be running.
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app import main as gateway

# `import app.main` is run as a subprocess from the service root, so the
# fail-closed tests exercise the real boot path (`uvicorn app.main:app`) rather
# than a helper in isolation.
SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _boot_gateway_with_secret(value):
    """Import app.main in a fresh interpreter with API_GATEWAY_SHARED_SECRET set
    to `value` (removed entirely when None) -- i.e. exactly what happens when
    uvicorn starts against a misconfigured environment."""
    env = os.environ.copy()
    if value is None:
        env.pop("API_GATEWAY_SHARED_SECRET", None)
    else:
        env["API_GATEWAY_SHARED_SECRET"] = value

    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _fake_response(status_code=200, content=b'{"ok":true}', content_type="application/json"):
    resp = httpx.Response(
        status_code=status_code, content=content, headers={"content-type": content_type},
        request=httpx.Request("GET", "http://fake"),
    )
    return resp


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "api-gateway"


def test_unknown_path_returns_404(client):
    resp = client.get("/not-a-real-route")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "no_route_for_path"


def test_path_with_route_prefix_as_substring_does_not_false_match(client):
    """'/orders-export' shares the '/orders' prefix textually but is a
    different resource -- must not be routed to order-service as if it were
    '/orders/export'."""
    resp = client.get("/orders-export")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "no_route_for_path"


def test_orders_path_routes_to_order_service(client):
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = client.post("/orders", json={"items": [{"sku": "A", "quantity": 1}]})

    assert resp.status_code == 200
    called_url = mock_request.call_args.args[1]
    assert called_url == f"{gateway.ORDER_SERVICE_URL}/orders"


def test_products_path_routes_to_inventory_service(client):
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = client.get("/products/WIDGET-1")

    assert resp.status_code == 200
    called_url = mock_request.call_args.args[1]
    assert called_url == f"{gateway.INVENTORY_SERVICE_URL}/products/WIDGET-1"


def test_notifications_path_routes_to_notification_service(client):
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = client.get("/notifications/order-1")

    assert resp.status_code == 200
    called_url = mock_request.call_args.args[1]
    assert called_url == f"{gateway.NOTIFICATION_SERVICE_URL}/notifications/order-1"


def test_upstream_connection_error_returns_502(client):
    with patch.object(
        gateway._client, "request",
        new=AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    ):
        resp = client.get("/orders/some-id")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream_service_unavailable"


def test_upstream_timeout_returns_504(client):
    with patch.object(
        gateway._client, "request",
        new=AsyncMock(side_effect=httpx.TimeoutException("timed out")),
    ):
        resp = client.get("/orders/some-id")

    assert resp.status_code == 504
    assert resp.json()["detail"] == "upstream_service_timeout"


def test_upstream_error_status_is_passed_through(client):
    with patch.object(
        gateway._client, "request",
        new=AsyncMock(return_value=_fake_response(status_code=409, content=b'{"detail":"conflict"}')),
    ):
        resp = client.post("/orders", json={"items": []})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "conflict"


# ---------------------------------------------------------------------------
# Shared-secret gate: X-API-Key vs API_GATEWAY_SHARED_SECRET
#
# The gate is mandatory and fails closed -- there is no configuration in which
# it is a no-op. The suite's `client` fixture authenticates on every request
# (tests/conftest.py); `anonymous_client` sends no header at all; and the tests
# below import the app in a fresh interpreter with the variable removed/blanked
# to prove it refuses to start.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_app_refuses_to_start_when_shared_secret_is_missing_or_empty(secret):
    """Fail closed: a missing/blank API_GATEWAY_SHARED_SECRET must abort startup.

    This replaces the old (deleted) test that asserted requests pass through
    when the variable is unset -- that fail-open behaviour is exactly what this
    gate must never do again.
    """
    result = _boot_gateway_with_secret(secret)

    assert result.returncode != 0, (
        "the gateway started successfully without API_GATEWAY_SHARED_SECRET "
        "-- that is a fail-open regression"
    )
    assert "API_GATEWAY_SHARED_SECRET" in result.stderr


def test_app_starts_when_shared_secret_is_set():
    """The negative control for the test above: with a secret, boot succeeds."""
    result = _boot_gateway_with_secret("a-real-shared-secret")

    assert result.returncode == 0, result.stderr


def test_loaded_secret_is_not_empty():
    """Whatever the suite configured, the running app has a real secret."""
    assert gateway.SHARED_SECRET


def test_request_without_api_key_is_rejected(anonymous_client):
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = anonymous_client.get("/products/WIDGET-1")

    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"
    # Rejected before routing, so the request never reaches the upstream service.
    mock_request.assert_not_awaited()

    # /health stays reachable without the header (container healthchecks).
    assert anonymous_client.get("/health").status_code == 200


def test_request_with_wrong_api_key_is_rejected(anonymous_client):
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = anonymous_client.get("/products/WIDGET-1", headers={"X-API-Key": "not-the-shared-secret"})

    assert resp.status_code == 401
    mock_request.assert_not_awaited()


def test_request_with_correct_api_key_succeeds(client):
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = client.get("/products/WIDGET-1", headers={"X-API-Key": gateway.SHARED_SECRET})

    assert resp.status_code == 200

    forwarded = {k.lower() for k in mock_request.call_args.kwargs["headers"]}
    assert "x-api-key" not in forwarded, "the gateway must not forward its own secret upstream"
