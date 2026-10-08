"""
Aetherline API Gateway. Single public entry point: routes requests to the
internal services, which (aside from this gateway) are not exposed outside
the Docker/Kubernetes network. Deliberately thin -- no business logic here,
just routing, so it can't become a second place order/inventory rules live.

Public exposure is protected by two PARALLEL credentials -- either one on
its own is sufficient (see the gate comment below and app/auth.py):
X-API-Key (shared secret) for services/automation, and per-user JWT bearer
tokens for human callers.
"""
import hmac
import os

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
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


def _is_authorized(request: Request) -> bool:
    """True when the request carries EITHER valid credential.

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
        return True

    scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
    if separator and scheme.lower() == "bearer" and token:
        try:
            auth.decode_token(token)
        except jwt.InvalidTokenError:
            return False  # expired / wrong key / alg != HS256 -> 401
        return True
    return False


@app.middleware("http")
async def credential_gate(request: Request, call_next):
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    if not _is_authorized(request):
        return Response(
            content='{"detail":"unauthorized"}',
            status_code=401,
            media_type="application/json",
        )

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
def register(credentials: schemas.UserCredentials, db: Session = Depends(get_db)):
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
def login(credentials: schemas.UserCredentials, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == credentials.email).first()
    if user is None:
        # Unknown email: still burn one bcrypt verification against a dummy
        # hash so the response timing is the same as a wrong password and
        # account existence cannot be probed.
        auth.verify_password(credentials.password, auth.DUMMY_PASSWORD_HASH)
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not auth.verify_password(credentials.password, user.password_hash):
        # Identical body for wrong-password and unknown-email, on purpose.
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"access_token": auth.create_token(user.id, user.email), "token_type": "bearer"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    full_path = f"/{path}"
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
            headers={
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("host", "content-length", "x-api-key", "authorization")
            },
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

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type"),
    )
