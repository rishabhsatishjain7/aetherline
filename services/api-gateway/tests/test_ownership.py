"""Item 1 ownership tests for the api-gateway."""
import json
from datetime import datetime, timedelta, timezone

import httpx
from unittest.mock import AsyncMock, patch

from app import auth as gateway_auth
from app import main as gateway
from app import models as gateway_models

VALID_PASSWORD = "correct-horse-battery"
REGISTER_EMAIL = "owner@example.com"


def _register(anonymous_client, email="owner@example.com", password="correct-horse-battery"):
    return anonymous_client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )


def _get_order(anonymous_client, token, order_id="order-123"):
    upstream = httpx.Response(
        200,
        content=json.dumps({"id": order_id, "status": "PENDING"}).encode("utf-8"),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", f"http://fake/orders/{order_id}"),
    )
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=upstream)) as mock_request:
        response = anonymous_client.get(f"/orders/{order_id}", headers={"Authorization": f"Bearer {token}"})
    return response, mock_request


def _get_notification(anonymous_client, token, order_id="order-123"):
    upstream = httpx.Response(
        200,
        content=json.dumps({"id": order_id, "status": "PENDING"}).encode("utf-8"),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", f"http://fake/notifications/{order_id}"),
    )
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=upstream)) as mock_request:
        response = anonymous_client.get(
            f"/notifications/{order_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    return response, mock_request


def _place_order(anonymous_client, token, client_key=None):
    headers = {"Authorization": f"Bearer {token}"}
    if client_key is not None:
        headers["Idempotency-Key"] = client_key

    with patch.object(gateway._client, "request", new=AsyncMock(return_value=FAKE_UPSTREAM)) as mock_request:
        response = anonymous_client.post(
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




def test_jwt_owner_gets_order_not_found_for_unknown_order(anonymous_client, db_session):
    token = _register(anonymous_client).json()["access_token"]

    response = anonymous_client.get(
        "/orders/never-owned-1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "order_not_found"}

def test_jwt_non_owner_gets_order_not_found(anonymous_client, db_session):
    _register(anonymous_client, email="owner@example.com").json()["access_token"]
    victim_token = _register(anonymous_client, email="victim@example.com").json()["access_token"]

    owner = db_session.query(gateway_models.User).filter(gateway_models.User.email == "owner@example.com").one()
    db_session.add(gateway_models.OrderOwnership(order_id="order-123", user_id=owner.id))
    db_session.commit()

    response = anonymous_client.get(
        "/orders/order-123",
        headers={"Authorization": f"Bearer {victim_token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "order_not_found"}


def test_jwt_owner_gets_own_notifications_200(anonymous_client, db_session):
    token = _register(anonymous_client).json()["access_token"]

    owner = db_session.query(gateway_models.User).filter(gateway_models.User.email == "owner@example.com").one()
    db_session.add(gateway_models.OrderOwnership(order_id="order-123", user_id=owner.id))
    db_session.commit()

    response, _ = _get_notification(anonymous_client, token)

    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"




def test_jwt_non_owner_gets_notification_not_found(anonymous_client, db_session):
    _register(anonymous_client, email="owner@example.com").json()["access_token"]
    victim_token = _register(anonymous_client, email="victim@example.com").json()["access_token"]

    owner = db_session.query(gateway_models.User).filter(gateway_models.User.email == "owner@example.com").one()
    db_session.add(gateway_models.OrderOwnership(order_id="order-123", user_id=owner.id))
    db_session.commit()

    response = anonymous_client.get(
        "/notifications/order-123",
        headers={"Authorization": f"Bearer {victim_token}"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "order_not_found"}


def test_service_caller_idempotency_key_is_forwarded_unmodified(anonymous_client):
    def fake_request(method, url, **kwargs):
        assert kwargs.get("headers", {}).get("Idempotency-Key") == "global-key"
        return httpx.Response(200, content=b'{"id":"order-1","status":"PENDING"}')

    with patch.object(gateway._client, "request", new=AsyncMock(side_effect=fake_request)):
        response = anonymous_client.post(
            "/orders",
            json={},
            headers={"X-API-Key": gateway.SHARED_SECRET, "Idempotency-Key": "global-key"},
        )

    assert response.status_code == 200


def test_jwt_caller_idempotency_key_is_namespaced(anonymous_client):
    token = _register(anonymous_client).json()["access_token"]
    user_id = gateway_auth.decode_token(token)["sub"]
    client_key = "client-key"

    def fake_request(method, url, **kwargs):
        assert kwargs.get("headers", {}).get("Idempotency-Key") == f"{user_id}:{client_key}"
        return httpx.Response(200, content=b'{"id":"order-1","status":"PENDING"}')

    with patch.object(gateway._client, "request", new=AsyncMock(side_effect=fake_request)):
        response = anonymous_client.post(
            "/orders",
            json={},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": client_key},
        )

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Step 3 coverage: ownership recording, 404 parity, idempotency namespacing,
# credential stripping, /my/orders, HTTP-level throttling.
# ---------------------------------------------------------------------------


def _token(anonymous_client, email):
    return _register(anonymous_client, email=email).json()["access_token"]


def _user_id(db_session, email):
    return db_session.query(gateway_models.User).filter(gateway_models.User.email == email).one().id


def _owned_order_ids(db_session, user_id):
    return {
        row.order_id
        for row in db_session.query(gateway_models.OrderOwnership)
        .filter(gateway_models.OrderOwnership.user_id == user_id)
        .all()
    }


def _login(client, email, password, xff=None):
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    return client.post("/auth/login", json={"email": email, "password": password}, headers=headers)


def _upstream(status_code=200, body=b'{"id":"order-123","status":"PENDING"}'):
    return httpx.Response(
        status_code,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "http://fake/orders"),
    )


def _post_orders(anonymous_client, headers, upstream=None):
    """POST /orders against a stubbed upstream; returns (response, mock)."""
    stub = upstream if upstream is not None else _upstream()
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=stub)) as mock_request:
        response = _post_orders_unpatched(anonymous_client, headers)
    return response, mock_request


def _post_orders_unpatched(anonymous_client, headers):
    """POST /orders with NO patching of the upstream client.

    Use this inside a `with patch.object(...)` block: _post_orders applies its
    own patch, which would shadow (and therefore silently disable) an outer
    one and make the assertions in the caller's stub never run.
    """
    return anonymous_client.post(
        "/orders",
        json={"items": [{"sku": "SKU-1", "quantity": 1}]},
        headers=headers,
    )


# --- Item 1: ownership is recorded only for JWT callers on a 2xx -----------


def test_ownership_row_recorded_for_jwt_caller_on_2xx_post_orders(anonymous_client, db_session):
    token = _token(anonymous_client, "owner@example.com")
    user_id = _user_id(db_session, "owner@example.com")

    response, _ = _post_orders(anonymous_client, {"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert _owned_order_ids(db_session, user_id) == {"order-123"}


def test_no_ownership_row_for_api_key_caller(anonymous_client, db_session):
    response, _ = _post_orders(anonymous_client, {"X-API-Key": gateway.SHARED_SECRET})

    assert response.status_code == 200
    assert db_session.query(gateway_models.OrderOwnership).count() == 0


def test_no_ownership_row_for_non_2xx_jwt_response(anonymous_client, db_session):
    token = _token(anonymous_client, "owner@example.com")
    user_id = _user_id(db_session, "owner@example.com")

    response, _ = _post_orders(
        anonymous_client,
        {"Authorization": f"Bearer {token}"},
        upstream=_upstream(503, b'{"detail":"upstream unavailable"}'),
    )

    assert response.status_code == 503
    assert _owned_order_ids(db_session, user_id) == set()
    assert db_session.query(gateway_models.OrderOwnership).count() == 0


def test_existing_owner_is_never_overwritten(anonymous_client, db_session):
    """Two callers whose orders come back with the SAME id: the first
    ownership row wins and the duplicate insert's IntegrityError is swallowed,
    so the second request still returns 2xx and nothing is reassigned."""
    first = _token(anonymous_client, "first@example.com")
    second = _token(anonymous_client, "second@example.com")
    first_id = _user_id(db_session, "first@example.com")
    second_id = _user_id(db_session, "second@example.com")

    first_response, _ = _post_orders(anonymous_client, {"Authorization": f"Bearer {first}"})
    second_response, _ = _post_orders(anonymous_client, {"Authorization": f"Bearer {second}"})

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    rows = (
        db_session.query(gateway_models.OrderOwnership)
        .filter(gateway_models.OrderOwnership.order_id == "order-123")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].user_id == first_id
    assert _owned_order_ids(db_session, second_id) == set()



# --- Item 2: non-owner and unknown order are indistinguishable -------------


def test_non_owner_and_unknown_order_return_byte_identical_404_bodies(anonymous_client, db_session):
    _token(anonymous_client, "owner@example.com")
    victim_token = _token(anonymous_client, "victim@example.com")
    db_session.add(
        gateway_models.OrderOwnership(
            order_id="owned-order", user_id=_user_id(db_session, "owner@example.com")
        )
    )
    db_session.commit()

    bodies = {}
    for order_id in ("owned-order", "no-such-order"):
        for method, path in (
            ("GET", f"/orders/{order_id}"),
            ("GET", f"/orders/{order_id}/events"),
            ("POST", f"/orders/{order_id}/cancel"),
            ("GET", f"/notifications/{order_id}"),
        ):
            with patch.object(gateway._client, "request", new=AsyncMock(return_value=_upstream())):
                response = anonymous_client.request(
                    method, path, headers={"Authorization": f"Bearer {victim_token}"}
                )
            assert response.status_code == 404, (method, path)
            bodies[(method, path, order_id)] = response.content

    # Non-owner vs. nonexistent are the same bytes on the same method+path:
    # no existence oracle leaks through the 404.
    for method, path in (
        ("GET", "/orders/{0}"),
        ("GET", "/orders/{0}/events"),
        ("POST", "/orders/{0}/cancel"),
        ("GET", "/notifications/{0}"),
    ):
        owned_body = bodies[(method, path.format("owned-order"), "owned-order")]
        unknown_body = bodies[(method, path.format("no-such-order"), "no-such-order")]
        assert owned_body == unknown_body, (method, path)
        assert owned_body == b'{"detail":"order_not_found"}', (method, path)


def test_non_owner_request_never_reaches_the_upstream_stub(anonymous_client, db_session):
    _token(anonymous_client, "owner@example.com")
    victim_token = _token(anonymous_client, "victim@example.com")
    db_session.add(
        gateway_models.OrderOwnership(
            order_id="owned-order", user_id=_user_id(db_session, "owner@example.com")
        )
    )
    db_session.commit()

    for method, path in (
        ("GET", "/orders/owned-order"),
        ("GET", "/orders/owned-order/events"),
        ("POST", "/orders/owned-order/cancel"),
        ("GET", "/notifications/owned-order"),
    ):
        with patch.object(gateway._client, "request", new=AsyncMock(return_value=_upstream())) as mock_request:
            response = anonymous_client.request(
                method, path, headers={"Authorization": f"Bearer {victim_token}"}
            )
        assert response.status_code == 404, (method, path)
        mock_request.assert_not_awaited()


# --- Item 3: idempotency keys are namespaced per user ----------------------


def test_same_idempotency_key_from_two_users_arrives_upstream_namespaced(anonymous_client, db_session):
    """order-service dedupes Idempotency-Key globally, so the gateway must
    namespace it per user: two callers sending the same key must produce two
    DIFFERENT upstream keys."""
    user_a = _token(anonymous_client, "a@example.com")
    user_b = _token(anonymous_client, "b@example.com")
    a_id = _user_id(db_session, "a@example.com")
    b_id = _user_id(db_session, "b@example.com")

    seen_keys = []

    def fake_request(method, url, **kwargs):
        seen_keys.append(kwargs["headers"]["Idempotency-Key"])
        return _upstream()

    with patch.object(gateway._client, "request", new=AsyncMock(side_effect=fake_request)):
        first = _post_orders_unpatched(anonymous_client, {"Authorization": f"Bearer {user_a}", "Idempotency-Key": "shared-key"})
        second = _post_orders_unpatched(anonymous_client, {"Authorization": f"Bearer {user_b}", "Idempotency-Key": "shared-key"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert seen_keys == [f"{a_id}:shared-key", f"{b_id}:shared-key"]
    assert len(set(seen_keys)) == 2


def test_second_user_never_receives_the_first_users_order(anonymous_client, db_session):
    """A distinct upstream key means the stub cannot replay A's order to B:
    each caller's 2xx carries its own order id."""
    user_a = _token(anonymous_client, "a@example.com")
    user_b = _token(anonymous_client, "b@example.com")
    a_id = _user_id(db_session, "a@example.com")
    b_id = _user_id(db_session, "b@example.com")

    responses = {
        f"{a_id}:shared-key": _upstream(body=b'{"id":"order-a","status":"PENDING"}'),
        f"{b_id}:shared-key": _upstream(body=b'{"id":"order-b","status":"PENDING"}'),
    }

    def fake_request(method, url, **kwargs):
        return responses[kwargs["headers"]["Idempotency-Key"]]

    with patch.object(gateway._client, "request", new=AsyncMock(side_effect=fake_request)):
        first = _post_orders_unpatched(
            anonymous_client, {"Authorization": f"Bearer {user_a}", "Idempotency-Key": "shared-key"}
        )
        second = _post_orders_unpatched(
            anonymous_client, {"Authorization": f"Bearer {user_b}", "Idempotency-Key": "shared-key"}
        )

    assert first.status_code == 200
    assert second.status_code == 200
    # The per-user key above is what makes these two different responses: the
    # stub cannot replay A's order to B because B asked a different question.
    assert first.json()["id"] == "order-a"
    assert second.json()["id"] == "order-b"
    assert _owned_order_ids(db_session, a_id) == {"order-a"}
    assert _owned_order_ids(db_session, b_id) == {"order-b"}


# --- Item 4: credentials are stripped before forwarding --------------------


def test_upstream_stub_receives_neither_authorization_nor_x_api_key(anonymous_client):
    token = _token(anonymous_client, "owner@example.com")

    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update({k.lower(): v for k, v in kwargs["headers"].items()})
        return _upstream()

    with patch.object(gateway._client, "request", new=AsyncMock(side_effect=fake_request)):
        jwt_resp, _ = _post_orders(anonymous_client, {"Authorization": f"Bearer {token}"})
        key_resp, _ = _post_orders(anonymous_client, {"X-API-Key": gateway.SHARED_SECRET})

    assert jwt_resp.status_code == 200
    assert key_resp.status_code == 200
    assert "authorization" not in seen
    assert "x-api-key" not in seen


# --- Item 5: Idempotency-Key length ---------------------------------------


def test_idempotency_key_of_129_chars_is_rejected_and_never_reaches_upstream(anonymous_client):
    token = _token(anonymous_client, "owner@example.com")

    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_upstream())) as mock_request:
        resp = anonymous_client.post(
            "/orders",
            json={"items": [{"sku": "SKU-1", "quantity": 1}]},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k" * 129},
        )

    assert resp.status_code == 422
    assert resp.json() == {"detail": "idempotency key too long"}
    mock_request.assert_not_awaited()


def test_idempotency_key_of_exactly_128_chars_is_accepted(anonymous_client):
    token = _token(anonymous_client, "owner@example.com")
    user_id = gateway_auth.decode_token(token)["sub"]
    key = "k" * 128

    def fake_request(method, url, **kwargs):
        assert kwargs["headers"]["Idempotency-Key"] == f"{user_id}:{key}"
        return _upstream()

    with patch.object(gateway._client, "request", new=AsyncMock(side_effect=fake_request)):
        resp = anonymous_client.post(
            "/orders",
            json={"items": [{"sku": "SKU-1", "quantity": 1}]},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        )

    assert resp.status_code == 200

# --- Item 6: GET /my/orders ------------------------------------------------


def _seed_orders(db_session, user_id, count, prefix="order"):
    rows = []
    for i in range(count):
        order_id = f"{prefix}-{i}"
        db_session.add(gateway_models.OrderOwnership(order_id=order_id, user_id=user_id))
        rows.append(order_id)
    db_session.commit()
    return rows


def _get_my_orders(anonymous_client, token, upstream_for):
    """GET /my/orders with a per-order-id stub; returns (response, called_ids)."""
    called_ids = []

    async def fake_get(url, **kwargs):
        order_id = url.rsplit("/", 1)[-1]
        called_ids.append(order_id)
        return upstream_for(order_id)

    with patch.object(gateway._client, "get", new=AsyncMock(side_effect=fake_get)):
        response = anonymous_client.get("/my/orders", headers={"Authorization": f"Bearer {token}"})
    return response, called_ids


def _json_upstream(order_id, status="PENDING"):
    return _upstream(body=json.dumps({"id": order_id, "status": status}).encode())


def test_my_orders_requires_a_jwt(anonymous_client):
    with patch.object(gateway._client, "get", new=AsyncMock(return_value=_upstream())) as mock_get:
        resp = anonymous_client.get("/my/orders", headers={"X-API-Key": gateway.SHARED_SECRET})

    assert resp.status_code == 403
    assert resp.json() == {"detail": "jwt required"}
    mock_get.assert_not_awaited()


def test_my_orders_returns_only_the_callers_own_orders(anonymous_client, db_session):
    token = _token(anonymous_client, "me@example.com")
    me = _user_id(db_session, "me@example.com")
    _token(anonymous_client, "other@example.com")
    other = _user_id(db_session, "other@example.com")
    mine = _seed_orders(db_session, me, 3, prefix="mine")
    _seed_orders(db_session, other, 2, prefix="theirs")

    resp, called = _get_my_orders(anonymous_client, token, _json_upstream)

    assert resp.status_code == 200
    assert sorted(entry["order_id"] for entry in resp.json()) == sorted(mine)
    assert set(called) == set(mine)


def test_my_orders_is_capped_at_20_newest_first(anonymous_client, db_session):
    token = _token(anonymous_client, "me@example.com")
    me = _user_id(db_session, "me@example.com")

    for i in range(25):
        db_session.add(gateway_models.OrderOwnership(order_id=f"order-{i:02d}", user_id=me))
    db_session.commit()
    # Backdate created_at so "newest first" is a real assertion about the
    # ordering, not an accident of insertion/id order.
    rows = db_session.query(gateway_models.OrderOwnership).order_by(
        gateway_models.OrderOwnership.order_id.asc()
    ).all()
    for offset, row in enumerate(rows):
        row.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=offset)
    db_session.commit()

    resp, called = _get_my_orders(anonymous_client, token, _json_upstream)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 20
    expected = [f"order-{i:02d}" for i in range(24, 4, -1)]
    assert [entry["order_id"] for entry in body] == expected
    assert called == expected


def test_my_orders_one_upstream_failure_yields_unknown_and_still_200(anonymous_client, db_session):
    token = _token(anonymous_client, "me@example.com")
    me = _user_id(db_session, "me@example.com")
    _seed_orders(db_session, me, 3, prefix="order")

    def upstream_for(order_id):
        if order_id == "order-1":
            return httpx.Response(
                500,
                content=b'{"detail":"boom"}',
                headers={"content-type": "application/json"},
                request=httpx.Request("GET", "http://fake"),
            )
        return _json_upstream(order_id)

    resp, _ = _get_my_orders(anonymous_client, token, upstream_for)

    assert resp.status_code == 200
    assert [entry for entry in resp.json() if entry["order_id"] == "order-1"] == [
        {"order_id": "order-1", "status": "UNKNOWN"}
    ]


def test_my_orders_non_json_upstream_body_yields_unknown(anonymous_client, db_session):
    token = _token(anonymous_client, "me@example.com")
    me = _user_id(db_session, "me@example.com")
    _seed_orders(db_session, me, 1, prefix="order")

    resp, _ = _get_my_orders(anonymous_client, token, lambda order_id: _upstream(body=b"<html>not json</html>"))

    assert resp.status_code == 200
    assert resp.json() == [{"order_id": "order-0", "status": "UNKNOWN"}]


def test_my_orders_upstream_exception_yields_unknown(anonymous_client, db_session):
    token = _token(anonymous_client, "me@example.com")
    me = _user_id(db_session, "me@example.com")
    _seed_orders(db_session, me, 1, prefix="order")

    async def boom(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    with patch.object(gateway._client, "get", new=AsyncMock(side_effect=boom)):
        resp = anonymous_client.get("/my/orders", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json() == [{"order_id": "order-0", "status": "UNKNOWN"}]


def test_my_orders_is_empty_for_a_user_with_no_orders(anonymous_client):
    token = _token(anonymous_client, "me@example.com")

    with patch.object(gateway._client, "get", new=AsyncMock(return_value=_upstream())) as mock_get:
        resp = anonymous_client.get("/my/orders", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json() == []
    mock_get.assert_not_awaited()



# --- Item 7: HTTP-level throttling ----------------------------------------


def test_http_login_allows_five_failures_then_returns_429_with_retry_after(anonymous_client):
    _register(anonymous_client)

    for i in range(gateway_auth.LOGIN_MAX_FAILURES):
        resp = _login(anonymous_client, REGISTER_EMAIL, "wrong-password")
        assert resp.status_code == 401, i

    resp = _login(anonymous_client, REGISTER_EMAIL, "wrong-password")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0


def test_http_login_throttle_is_per_email_across_different_ips(anonymous_client):
    _register(anonymous_client)

    for i in range(gateway_auth.LOGIN_MAX_FAILURES):
        resp = _login(anonymous_client, REGISTER_EMAIL, "wrong-password", xff=f"198.51.100.{i + 1}")
        assert resp.status_code == 401, i

    # A never-before-seen source address must not buy a fresh bucket: the
    # per-EMAIL count is already at the limit, and the header is ignored.
    resp = _login(anonymous_client, REGISTER_EMAIL, "wrong-password", xff="198.51.100.250")
    assert resp.status_code == 429


def test_http_login_success_for_one_email_is_not_blocked_by_another_emails_failures(anonymous_client):
    """Per-EMAIL bucketing, verified from the same peer IP: an attacker's
    failures below the limit must not block an unrelated account."""
    _register(anonymous_client, email="attacker@example.com")
    _register(anonymous_client, email="bystander@example.com")

    # One short of the limit: the attacker is not throttled yet, and the
    # bystander must be unaffected entirely (separate email bucket).
    for _ in range(gateway_auth.LOGIN_MAX_FAILURES - 1):
        assert _login(anonymous_client, "attacker@example.com", "wrong-password").status_code == 401

    resp = _login(anonymous_client, "bystander@example.com", VALID_PASSWORD)
    assert resp.status_code == 200

    # The attacker's next failure trips its own bucket; the bystander is still
    # not throttled, because only that one email reached the limit.
    assert _login(anonymous_client, "attacker@example.com", "wrong-password").status_code == 401
    assert _login(anonymous_client, "attacker@example.com", "wrong-password").status_code == 429
    assert _login(anonymous_client, "bystander@example.com", VALID_PASSWORD).status_code == 200


def test_http_register_returns_429_with_retry_after_after_the_limit(anonymous_client):
    for i in range(gateway_auth.REGISTER_MAX_ATTEMPTS_PER_IP):
        resp = _register(anonymous_client, email=f"user{i}@example.com")
        assert resp.status_code == 200, i

    resp = _register(anonymous_client, email="one-too-many@example.com")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0


# --- Item 8: the autouse reset fixture is what isolates these tests --------


def test_autouse_fixture_resets_throttle_buckets_before_every_test(anonymous_client):
    """Whatever an earlier test left behind, every bucket starts empty -- the
    isolation comes from conftest's autouse fixture, not from looser limits."""
    assert gateway_auth._login_failures_by_ip == {}
    assert gateway_auth._login_failures_by_email == {}
    assert gateway_auth._register_attempts_by_ip == {}

    # Fill the whole register allowance, so a leaked bucket would be visible
    # to the test below.
    for i in range(gateway_auth.REGISTER_MAX_ATTEMPTS_PER_IP):
        assert _register(anonymous_client, email=f"leak{i}@example.com").status_code == 200
    assert len(gateway_auth._register_attempts_by_ip) == 1


def test_autouse_fixture_emptyed_the_buckets_the_previous_test_filled():
    """Only passes if the autouse fixture cleared the buckets again."""
    assert gateway_auth._register_attempts_by_ip == {}
    assert gateway_auth._login_failures_by_ip == {}
    assert gateway_auth._login_failures_by_email == {}

