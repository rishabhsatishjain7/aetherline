"""Item 1 ownership tests for the api-gateway."""
import json

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from app import auth as gateway_auth
from app import main as gateway
from app import models as gateway_models


def _register(anonymous_client, email="owner@example.com", password="correct-horse-battery"):
    return anonymous_client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )


def _get_order(client, token, order_id="order-123"):
    upstream = httpx.Response(
        200,
        content=json.dumps({"id": order_id, "status": "PENDING"}).encode("utf-8"),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", f"http://fake/orders/{order_id}"),
    )
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=upstream)) as mock_request:
        response = client.get(f"/orders/{order_id}", headers={"Authorization": f"Bearer {token}"})
    return response, mock_request


def _get_notification(client, token, order_id="order-123"):
    upstream = httpx.Response(
        200,
        content=json.dumps({"id": order_id, "status": "PENDING"}).encode("utf-8"),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", f"http://fake/notifications/{order_id}"),
    )
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=upstream)) as mock_request:
        response = client.get(
            f"/notifications/{order_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    return response, mock_request


def _place_order(client, token, client_key=None):
    headers = {"Authorization": f"Bearer {token}"}
    if client_key is not None:
        headers["Idempotency-Key"] = client_key

    with patch.object(gateway._client, "request", new=AsyncMock(return_value=FAKE_UPSTREAM)) as mock_request:
        response = client.post(
            "/orders",
            json={"items": [{"sku": "SKU-1", "quantity": 1}]},
            headers=headers,
        )
    return response, mock_request


FAKE_UPSTREAM = httpx.Response(
    200,
    content=json.dumps({"id": "order-123", "status": "PENDING"}).encode("utf-8"),
    headers={"content-type": "application/json"},
    request=httpx.Request("POST", "http://fake/orders"),
)




def test_jwt_owner_gets_order_not_found_for_unknown_order(client, db_session):
    token = _register(anonymous_client=client).json()["access_token"]

    response = client.get(
        "/orders/never-owned-1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "order_not_found"}

def test_jwt_non_owner_gets_order_not_found(client, db_session):
    owner_token = _register(anonymous_client=client).json()["access_token"]
    victim_token = _register(anonymous_client=client).json()["access_token"]

    owner = db_session.query(gateway_models.User).filter(gateway_models.User.email == "owner@example.com").one()
    db_session.add(gateway_models.OrderOwnership(order_id="order-123", user_id=owner.id))
    db_session.commit()

    response = client.get(
        "/orders/order-123",
        headers={"Authorization": f"Bearer {victim_token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "order_not_found"}


def test_jwt_owner_gets_own_notifications_200(client, db_session):
    token = _register(anonymous_client=client).json()["access_token"]

    owner = db_session.query(gateway_models.User).filter(gateway_models.User.email == "owner@example.com").one()
    db_session.add(gateway_models.OrderOwnership(order_id="order-123", user_id=owner.id))
    db_session.commit()

    response, _ = _get_notification(client, token)

    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"




def test_jwt_non_owner_gets_notification_not_found(client, db_session):
    owner_token = _register(anonymous_client=client).json()["access_token"]
    victim_token = _register(anonymous_client=client).json()["access_token"]

    owner = db_session.query(gateway_models.User).filter(gateway_models.User.email == "owner@example.com").one()
    db_session.add(gateway_models.OrderOwnership(order_id="order-123", user_id=owner.id))
    db_session.commit()

    response = client.get(
        "/notifications/order-123",
        headers={"Authorization": f"Bearer {victim_token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "order_not_found"}


def test_service_caller_idempotency_key_is_forwarded_unmodified(client):
    def fake_request(method, url, **kwargs):
        assert kwargs.get("headers", {}).get("Idempotency-Key") == "global-key"
        return _fake_response(200, b'{"id":"order-1","status":"PENDING"}')

    with patch.object(gateway._client, "request", new=AsyncMock(side_effect=fake_request)):
        response = client.post(
            "/orders",
            json={},
            headers={"X-API-Key": gateway.SHARED_SECRET, "Idempotency-Key": "global-key"},
        )

    assert response.status_code == 200


def test_jwt_caller_idempotency_key_is_namespaced(client):
    token = _register(anonymous_client=client).json()["access_token"]
    user_id = gateway_auth.decode_token(token)["sub"]
    client_key = "client-key"

    def fake_request(method, url, **kwargs):
        assert kwargs.get("headers", {}).get("Idempotency-Key") == f"{user_id}:{client_key}"
        return _fake_response(200, b'{"id":"order-1","status":"PENDING"}')

    with patch.object(gateway._client, "request", new=AsyncMock(side_effect=fake_request)):
        response = client.post(
            "/orders",
            json={},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": client_key},
        )

    assert response.status_code == 200
