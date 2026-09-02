"""
Tests the gateway's routing and error-translation logic by mocking the
outbound httpx client -- no real order-service/inventory-service etc. need
to be running.
"""
import httpx
from unittest.mock import AsyncMock, patch
from app import main as gateway


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
