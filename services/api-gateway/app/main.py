"""
Aetherline API Gateway. Single public entry point: routes requests to the
internal services, which (aside from this gateway) are not exposed outside
the Docker/Kubernetes network. Deliberately thin -- no business logic here,
just routing, so it can't become a second place order/inventory rules live.

Public exposure is protected by two PARALLEL credentials -- either one on
its own is sufficient (see the gate comment below and app/auth.py):
X-API-Key (shared secret) for services/automation, and per-user JWT bearer
tokens for human callers. Phase 2 adds authorization on top: JWT callers
are customers limited to the route allowlist in auth.authorize plus the
orders they own (order_ownership table); API-key callers keep full
access. Login/register are throttled per-instance in memory.
"""
import asyncio
import hmac
import json
import os

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth, models, schemas
from .database import engine, get_db

app = FastAPI(title="Aetherline - API Gateway")
Instrumentator().instrument(app).expose(app)

# ---------------------------------------------------------------------------
# Shared-secret gate (X-API-Key) -- FAIL CLOSED
#
# One shared secret, compared against the `X-API-Key` header on every route
# except the explicitly unauthenticated ones below. This is the service /
# automation credential: the CD smoke test, Prometheus and scripts use it.
#
# It is NOT the credential human users are meant to hold -- that is a
# per-user JWT (POST /auth/register, POST /auth/login against gateway-db);
# see app/auth.py. Either credential is sufficient on a protected route.
#
# FAIL-CLOSED BY DESIGN: API_GATEWAY_SHARED_SECRET is mandatory. If it is
# missing or blank the module raises at import time, so uvicorn exits and the
# container never starts -- a misconfiguration is a loud boot failure, never a
# silent loss of authentication. There is deliberately no configuration in
# which this gate is a no-op. Local Docker Compose supplies an explicit
# non-secret placeholder (docker-compose.yml); the AWS deployment injects a
# real random secret via k8s/overlays/aws/gateway-auth.yaml. JWT_SECRET_KEY
# fails closed the same way (app/auth.py).
#
# Note: /metrics is gated too, so anything scraping the gateway (the local
# Prometheus in observability/prometheus.yml) has to send the header. Nothing
# scrapes the gateway's /metrics in the AWS deployment today.
# ---------------------------------------------------------------------------
# The only paths reachable with no credential at all: the container
# healthcheck, and the two endpoints that mint credentials in the first
# place. Everything else -- proxied or not -- must pass the gate below.
_AUTH_EXEMPT_PATHS = frozenset({"/health", "/auth/register", "/auth/login"})


def _load_shared_secret() -> str:
    """Return the configured shared secret, or refuse to start.

    Called at import time on purpose: raising here is what makes the gateway
    fail closed, because uvicorn (and therefore the container) never gets the
    chance to serve a single request with the gate disabled.
    """
    secret = os.getenv("API_GATEWAY_SHARED_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "API_GATEWAY_SHARED_SECRET is missing or empty -- refusing to "
            "start. The api-gateway fails closed by design: starting without a "
            "shared secret would silently serve unauthenticated traffic. Set "
            "it to a long random value (e.g. `openssl rand -hex 32`) in any "
            "real deployment, or to the non-secret local-dev placeholder from "
            "docker-compose.yml for local development."
        )
    return secret


# Read once, at import. A process's environment cannot change after startup, so
# caching here (a) keeps requests from ever being evaluated against a different
# value than the one validated at boot, and (b) makes the fail-closed guarantee
# below structural rather than a per-request check.
SHARED_SECRET = _load_shared_secret()

# Same startup behaviour as the other four services: create this service's
# tables on boot (the `users` table lives in gateway-db). Runs only after
# BOTH mandatory credentials have loaded, so a misconfigured deployment
# never gets as far as touching the database.
models.Base.metadata.create_all(bind=engine)


def _authenticate(request: Request) -> auth.Principal | None:
    """Return the caller's Principal when EITHER credential is valid.

    Both are sufficient on their own by design:
      * a correct X-API-Key (constant-time compared), for services and
        automation such as the CD smoke test;
      * a valid, unexpired HS256 JWT (strict decode: algorithms pinned to
        HS256, `exp` required) for a human user who registered/logged in.
    Anything else -- missing, wrong, expired, or wrongly signed -- is not
    authorized; the middleware below turns that into a 401.
    """
    presented = request.headers.get("X-API-Key", "")
    # Constant-time comparison so the header can't be brute-forced by timing.
    # `not SHARED_SECRET` is belt-and-braces -- it is already guaranteed
    # non-empty above, but comparing "" == "" must never be able to let a
    # request through.
    if presented and SHARED_SECRET and hmac.compare_digest(presented, SHARED_SECRET):
        return auth.Principal(kind="service")

    scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
    if separator and scheme.lower() == "bearer" and token:
        try:
            claims = auth.decode_token(token)
        except jwt.PyJWTError:
            return None  # expired / wrong key / alg != HS256 -> 401
        sub = claims.get("sub")
        if not sub or not isinstance(sub, str):
            return None  # structurally valid token but no usable subject
        return auth.Principal(kind="jwt", user_id=sub)
    return None


@app.middleware("http")
async def credential_gate(request: Request, call_next):
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    principal = _authenticate(request)
    if not isinstance(principal, auth.Principal):
        return Response(
            content='{"detail":"unauthorized"}',
            status_code=401,
            media_type="application/json",
        )

    request.scope["principal"] = principal
    return await call_next(request)


ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8000")
INVENTORY_SERVICE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://inventory-service:8000")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://notification-service:8000")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8000")

_client = httpx.AsyncClient(timeout=10.0)

_ROUTES = [
    ("/orders", ORDER_SERVICE_URL),
    ("/products", INVENTORY_SERVICE_URL),
    ("/inventory", INVENTORY_SERVICE_URL),
    ("/notifications", NOTIFICATION_SERVICE_URL),
    ("/payments", PAYMENT_SERVICE_URL),
]


def _require_principal(request: Request) -> auth.Principal:
    principal = request.scope.get("principal")
    if principal is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return principal

TRUSTED_PROXY_HOPS_ENV = "TRUSTED_PROXY_HOPS"


def _trusted_proxy_hops() -> int:
    """How many proxy hops sit in front of the gateway (TRUSTED_PROXY_HOPS).

    Default 0 means the gateway is assumed to be reached directly, so the
    only trustworthy peer address is the TCP peer (request.client.host).
    Anything unparseable, negative or fractional is treated as 0 -- there is
    deliberately no configuration in which a client-controlled header can
    influence throttle bucketing.
    """
    try:
        hops = int(os.getenv(TRUSTED_PROXY_HOPS_ENV, "0"))
    except ValueError:
        return 0
    return hops if hops > 0 else 0


# Resolved once at import, like the two secrets, and read through the module
# global by _client_ip below so there is exactly one value per process.
TRUSTED_PROXY_HOPS = _trusted_proxy_hops()


def _client_ip(request: Request) -> str:
    """Best-effort client IP, used ONLY for throttle bucketing.

    X-Forwarded-For is read from the RIGHT, never the left, and only when
    TRUSTED_PROXY_HOPS says how many proxies to skip. The leftmost entry is
    attacker-controlled: any client can send `X-Forwarded-For: 1.2.3.4` and
    rewrite it, so trusting it would let anyone mint a fresh throttle bucket
    per request and walk straight through the login/register limits. Each
    trusted proxy APPENDS the address it saw, so the rightmost entries are
    the ones actually written by infrastructure this deployment owns. With
    the default of 0 hops the header is ignored entirely, so behind a shared
    load balancer every client looks like the same IP until the deployment
    sets TRUSTED_PROXY_HOPS -- deliberately erring towards over-throttling
    real users rather than under-throttling an attacker.
    """
    hops = TRUSTED_PROXY_HOPS
    if hops > 0:
        forwarded = [
            entry.strip()
            for entry in request.headers.get("x-forwarded-for", "").split(",")
            if entry.strip()
        ]
        if len(forwarded) >= hops:
            # Negative index counts from the right: the Nth entry from the
            # right is the address the outermost trusted proxy observed.
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


def _order_not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "order_not_found"})


def _owns_order(db: Session, user_id: str, order_id: str) -> bool:
    return (
        db.query(models.OrderOwnership)
        .filter(
            models.OrderOwnership.order_id == order_id,
            models.OrderOwnership.user_id == user_id,
        )
        .first()
        is not None
    )


def _record_ownership(db: Session, user_id: str, order_id: str) -> None:
    db.add(models.OrderOwnership(order_id=order_id, user_id=user_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def _forward_headers(request: Request, principal) -> dict:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "x-api-key", "authorization")
    }
    if principal.kind == "jwt":
        client_key = request.headers.get("Idempotency-Key", "")
        if client_key:
            if len(client_key) > 128:
                raise HTTPException(status_code=422, detail="idempotency key too long")
            headers["Idempotency-Key"] = principal.user_id + ":" + client_key
    elif "Idempotency-Key" in request.headers:
        headers["Idempotency-Key"] = request.headers["Idempotency-Key"]
    return headers


@app.get("/health")
async def health():
    return {"status": "ok", "service": "api-gateway"}


# ---------------------------------------------------------------------------
# Per-user auth endpoints (Phase 1). Handled HERE, never proxied, and
# defined before the catch-all proxy route below so FastAPI matches them
# first. Both are unauthenticated by definition (see _AUTH_EXEMPT_PATHS).
# No passwords or hashes are ever echoed or logged, not even in errors.
# ---------------------------------------------------------------------------
@app.post("/auth/register")
def register(
    request: Request,
    credentials: schemas.UserCredentials,
    db: Session = Depends(get_db),
):
    if auth.register_throttled(_client_ip(request)):
        return JSONResponse(
            status_code=429,
            content={"detail": "too many registration attempts"},
            headers={"Retry-After": str(auth.LOGIN_WINDOW_SECONDS)},
        )
    # Checked by BYTE length, not characters: bcrypt operates on bytes and
    # rejects (or on older versions truncates) anything past 72. A pydantic
    # validator is deliberately NOT used for the password: pydantic's 422
    # body echoes the rejected input, which would echo the password back.
    if not 8 <= len(credentials.password.encode("utf-8")) <= 72:
        raise HTTPException(status_code=422, detail="password must be 8-72 bytes")

    user = models.User(
        email=credentials.email,
        password_hash=auth.hash_password(credentials.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Insert-first and let the unique constraint decide -- NOT
        # check-then-insert, which races (the pattern this repo already
        # removed from its other services).
        db.rollback()
        raise HTTPException(status_code=409, detail="email_already_exists")
    db.refresh(user)
    return {"access_token": auth.create_token(user.id, user.email), "token_type": "bearer"}


@app.post("/auth/login")
def login(
    request: Request,
    credentials: schemas.UserCredentials,
    db: Session = Depends(get_db),
):
    client_ip = _client_ip(request)
    email = credentials.email
    if auth.login_throttled(client_ip, email):
        return JSONResponse(
            status_code=429,
            content={"detail": "too many failed login attempts"},
            headers={"Retry-After": str(auth.LOGIN_WINDOW_SECONDS)},
        )
    user = db.query(models.User).filter(models.User.email == credentials.email).first()
    if user is None:
        # Unknown email: still burn one bcrypt verification against a dummy
        # hash so the response timing is the same as a wrong password and
        # account existence cannot be probed.
        auth.verify_password(credentials.password, auth.DUMMY_PASSWORD_HASH)
        auth.record_login_failure(client_ip, email)
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not auth.verify_password(credentials.password, user.password_hash):
        # Identical body for wrong-password and unknown-email, on purpose.
        auth.record_login_failure(client_ip, email)
        raise HTTPException(status_code=401, detail="invalid credentials")
    auth.clear_login_failures(client_ip, email)
    return {"access_token": auth.create_token(user.id, user.email), "token_type": "bearer"}


# ---------------------------------------------------------------------------
# GET /my/orders (JWT only, handled locally; defined before the proxy)
# ---------------------------------------------------------------------------
@app.get("/my/orders")
async def my_orders(request: Request, db: Session = Depends(get_db)):
    """The caller's most recent 20 owned orders, fetched concurrently."""
    principal = _require_principal(request)
    if principal.kind != "jwt":
        raise HTTPException(status_code=403, detail="jwt required")
    rows = (
        db.query(models.OrderOwnership)
        .filter(models.OrderOwnership.user_id == principal.user_id)
        .order_by(models.OrderOwnership.created_at.desc())
        .limit(20)
        .all()
    )

    async def _fetch(order_id: str) -> dict:
        try:
            resp = await _client.get(f"{ORDER_SERVICE_URL}/orders/{order_id}")
        except (httpx.ConnectError, httpx.TimeoutException):
            return {"order_id": order_id, "status": "UNKNOWN"}
        if resp.status_code != 200:
            return {"order_id": order_id, "status": "UNKNOWN"}
        try:
            data = json.loads(resp.content)
        except ValueError:
            return {"order_id": order_id, "status": "UNKNOWN"}
        if not isinstance(data, dict):
            # A 200 with a non-object body is a malformed upstream response:
            # treat it like any other fetch failure, never let it 500.
            return {"order_id": order_id, "status": "UNKNOWN"}
        return {"order_id": order_id, "status": data.get("status", "UNKNOWN")}

    return list(await asyncio.gather(*(_fetch(r.order_id) for r in rows)))

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request, db: Session = Depends(get_db)):
    """Authorize, then forward to the owning internal service."""
    principal = _require_principal(request)
    full_path = f"/{path}"

    # 1. Route allowlist for JWT principals (service callers bypass it).
    if not auth.authorize(principal, request.method, full_path):
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    # 2. Object-level ownership for JWT principals on order-scoped paths:
    #    non-owners get the same 404 body as a genuinely missing order.
    order_id = auth.extract_order_id(full_path) if principal.kind == "jwt" else None
    if order_id is not None and not _owns_order(db, principal.user_id or "", order_id):
        return _order_not_found()

    try:
        headers = _forward_headers(request, principal)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    target_base = next(
        (base for prefix, base in _ROUTES if full_path == prefix or full_path.startswith(prefix + "/")),
        None,
    )
    if target_base is None:
        return Response(content='{"detail":"no_route_for_path"}', status_code=404, media_type="application/json")

    upstream_url = f"{target_base}{full_path}"
    body = await request.body()

    try:
        upstream_resp = await _client.request(
            request.method,
            upstream_url,
            params=request.query_params,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        return Response(
            content='{"detail":"upstream_service_unavailable"}', status_code=502, media_type="application/json"
        )
    except httpx.TimeoutException:
        return Response(
            content='{"detail":"upstream_service_timeout"}', status_code=504, media_type="application/json"
        )

    # 3. Record ownership: a JWT POST /orders that came back 2xx links the
    #    new order id to the caller. Parsed from a COPY of the body; the
    #    original bytes are forwarded untouched below. API-key callers
    #    record nothing.
    if (
        principal.kind == "jwt"
        and request.method == "POST"
        and full_path == "/orders"
        and 200 <= upstream_resp.status_code < 300
    ):
        try:
            new_order_id = json.loads(upstream_resp.content).get("id")
        except ValueError:
            new_order_id = None
        if new_order_id:
            _record_ownership(db, principal.user_id or "", str(new_order_id))

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type"),
    )
