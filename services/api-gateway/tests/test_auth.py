"""
Per-user JWT auth tests: register/login against gateway-db, the two
parallel credentials on protected routes, and the fail-closed startup rule
for JWT_SECRET_KEY. Routing/mock behaviour of the proxy itself lives in
test_gateway.py.
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import jwt as pyjwt
import pytest
from app import auth as gateway_auth
from app import main as gateway

# `import app.main` is run as a subprocess from the service root, so the
# fail-closed tests exercise the real boot path rather than a helper.
SERVICE_ROOT = Path(__file__).resolve().parents[1]

VALID_PASSWORD = "correct-horse-battery"
REGISTER_EMAIL = "user@example.com"


def _register(anonymous_client, email=REGISTER_EMAIL, password=VALID_PASSWORD):
    return anonymous_client.post("/auth/register", json={"email": email, "password": password})


def _login(anonymous_client, email, password):
    return anonymous_client.post("/auth/login", json={"email": email, "password": password})


def _fake_response(status_code=200, content=b'{"ok":true}'):
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "http://fake"),
    )


def _claims(**overrides):
    now = int(time.time())
    base = {"sub": "user-1", "email": REGISTER_EMAIL, "iat": now, "exp": now + 600}
    base.update(overrides)
    return base


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _alg_none_token() -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(_claims(), separators=(",", ":")).encode())
    return f"{header}.{payload}."


# ---------------------------------------------------------------------------
# /auth/register
# ---------------------------------------------------------------------------


def test_register_success_returns_jwt_and_stores_bcrypt_hash(anonymous_client, db_session):
    resp = _register(anonymous_client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]

    # The token is a real, strictly-decodable JWT for this deployment.
    claims = gateway_auth.decode_token(body["access_token"])
    assert claims["email"] == REGISTER_EMAIL

    # Row exists with a bcrypt hash -- and the raw password is not echoed
    # anywhere in the response body.
    user = db_session.query(gateway.models.User).filter(gateway.models.User.email == REGISTER_EMAIL).one()
    assert user.password_hash.startswith("$2")
    assert VALID_PASSWORD not in resp.text


def test_register_duplicate_email_conflicts(anonymous_client):
    assert _register(anonymous_client).status_code == 200
    duplicate = _register(anonymous_client)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "email_already_exists"

    # Emails are normalized (lower-cased), so the unique constraint -- not a
    # pre-check -- is what rejects a case-variant duplicate too.
    case_variant = _register(anonymous_client, email="USER@Example.com")
    assert case_variant.status_code == 409


@pytest.mark.parametrize(
    "password",
    ["weakpw", "abc1234", "a" * 73],
    ids=["too-short-6bytes", "too-short-7bytes", "over-72-bytes"],
)
def test_register_rejects_passwords_outside_bcrypt_byte_limits(anonymous_client, password):
    resp = _register(anonymous_client, password=password)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "password must be 8-72 bytes"
    # The rejected password must not be echoed back (pydantic would).
    assert password not in resp.text


def test_register_rejects_invalid_email(anonymous_client):
    resp = _register(anonymous_client, email="not-an-email")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /auth/login
# ---------------------------------------------------------------------------


def test_login_success_returns_usable_jwt(anonymous_client):
    assert _register(anonymous_client).status_code == 200

    resp = _login(anonymous_client, REGISTER_EMAIL, VALID_PASSWORD)
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    assert gateway_auth.decode_token(token)["email"] == REGISTER_EMAIL

    # And that token actually unlocks a protected, proxied route.
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        proxied = anonymous_client.get("/products/WIDGET-1", headers={"Authorization": f"Bearer {token}"})
    assert proxied.status_code == 200
    mock_request.assert_awaited_once()


def test_login_wrong_password_and_unknown_email_return_identical_bodies(anonymous_client):
    assert _register(anonymous_client).status_code == 200

    wrong_password = _login(anonymous_client, REGISTER_EMAIL, "definitely-not-the-password")
    unknown_email = _login(anonymous_client, "nobody-here@example.com", "definitely-not-the-password")

    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401
    # Byte-identical: the body must not reveal whether the account exists.
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.json() == {"detail": "invalid credentials"}


# ---------------------------------------------------------------------------
# Protected routes: either credential is sufficient, neither/invalid is 401
# ---------------------------------------------------------------------------


def test_protected_route_accepts_valid_jwt(anonymous_client):
    token = _register(anonymous_client).json()["access_token"]

    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = anonymous_client.get("/products/WIDGET-1", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    # The user's bearer token is not forwarded upstream -- credential headers
    # stop at the gateway.
    forwarded = {k.lower() for k in mock_request.call_args.kwargs["headers"]}
    assert "authorization" not in forwarded


def test_protected_route_accepts_valid_x_api_key(anonymous_client):
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = anonymous_client.get("/products/WIDGET-1", headers={"X-API-Key": gateway.SHARED_SECRET})

    assert resp.status_code == 200
    forwarded = {k.lower() for k in mock_request.call_args.kwargs["headers"]}
    assert "x-api-key" not in forwarded, "the gateway must not forward its own secret upstream"
    mock_request.assert_awaited_once()


def test_protected_route_rejects_requests_with_no_credential(anonymous_client):
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = anonymous_client.get("/products/WIDGET-1")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
    mock_request.assert_not_awaited()


def test_protected_route_rejects_expired_jwt(anonymous_client):
    expired = pyjwt.encode(_claims(exp=int(time.time()) - 10), gateway_auth.JWT_SECRET_KEY, algorithm="HS256")

    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = anonymous_client.get("/products/WIDGET-1", headers={"Authorization": f"Bearer {expired}"})

    assert resp.status_code == 401
    mock_request.assert_not_awaited()


def test_protected_route_rejects_jwt_signed_by_a_different_key(anonymous_client):
    foreign = pyjwt.encode(_claims(), "some-other-deployments-secret", algorithm="HS256")

    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = anonymous_client.get("/products/WIDGET-1", headers={"Authorization": f"Bearer {foreign}"})

    assert resp.status_code == 401
    mock_request.assert_not_awaited()


def test_protected_route_rejects_alg_none_token(anonymous_client):
    with patch.object(gateway._client, "request", new=AsyncMock(return_value=_fake_response())) as mock_request:
        resp = anonymous_client.get("/products/WIDGET-1", headers={"Authorization": f"Bearer {_alg_none_token()}"})

    assert resp.status_code == 401
    mock_request.assert_not_awaited()


def test_health_stays_open_without_credentials(anonymous_client):
    assert anonymous_client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# JWT_SECRET_KEY fails closed at startup, exactly like the shared secret
# ---------------------------------------------------------------------------


def _boot_gateway_without_jwt(value):
    """Import app.main in a fresh interpreter with JWT_SECRET_KEY set to
    `value` (removed entirely when None) and a valid shared secret -- i.e.
    exactly what happens when uvicorn starts against a half-configured
    environment."""
    env = os.environ.copy()
    env["API_GATEWAY_SHARED_SECRET"] = "a-real-shared-secret"
    if value is None:
        env.pop("JWT_SECRET_KEY", None)
    else:
        env["JWT_SECRET_KEY"] = value

    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("secret", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_app_refuses_to_start_when_jwt_secret_is_missing_or_empty(secret):
    """Fail closed: a missing/blank JWT_SECRET_KEY must abort startup."""
    result = _boot_gateway_without_jwt(secret)

    assert result.returncode != 0, (
        "the gateway started successfully without JWT_SECRET_KEY -- that is "
        "a fail-open regression: every token would be unverifiable"
    )
    assert "JWT_SECRET_KEY" in result.stderr

