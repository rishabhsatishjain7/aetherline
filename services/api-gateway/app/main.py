"""
Aetherline API Gateway. Single public entry point: routes requests to the
internal services, which (aside from this gateway) are not exposed outside
the Docker/Kubernetes network. Deliberately thin -- no business logic here,
just routing, so it can't become a second place order/inventory rules live.

Public exposure is protected by a mandatory shared-secret gate (see below).
"""
import hmac
import os
import httpx
from fastapi import FastAPI, Request, Response

from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Aetherline - API Gateway")
Instrumentator().instrument(app).expose(app)

# ---------------------------------------------------------------------------
# Shared-secret gate -- FAIL CLOSED
#
# Deliberately minimal and demo-appropriate: one shared secret, compared
# against the `X-API-Key` header on every route except /health. This is NOT
# user authentication -- there is no login, no per-user identity, no JWT, no
# key rotation and no audit trail. It exists so the one container reachable
# from outside Docker/Kubernetes is never a wide-open gateway. A real
# production deployment would need proper JWT-based auth per user; see
# CLAUDE.md.
#
# FAIL-CLOSED BY DESIGN: API_GATEWAY_SHARED_SECRET is mandatory. If it is
# missing or blank the module raises at import time, so uvicorn exits and the
# container never starts -- a misconfiguration is a loud boot failure, never a
# silent loss of authentication. There is deliberately no configuration in
# which this gate is a no-op. Local Docker Compose supplies an explicit
# non-secret placeholder (docker-compose.yml); the AWS deployment injects a
# real random secret via k8s/overlays/aws/gateway-auth.yaml.
#
# Note: /metrics is gated too, so anything scraping the gateway (the local
# Prometheus in observability/prometheus.yml) has to send the header. Nothing
# scrapes the gateway's /metrics in the AWS deployment today.
# ---------------------------------------------------------------------------
_AUTH_EXEMPT_PATHS = frozenset({"/health"})


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


@app.middleware("http")
async def shared_secret_gate(request: Request, call_next):
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    presented = request.headers.get("X-API-Key", "")
    # Constant-time comparison so the header can't be brute-forced by timing.
    # `not SHARED_SECRET` is belt-and-braces -- it is already guaranteed
    # non-empty above, but comparing "" == "" must never be able to let a
    # request through.
    if not SHARED_SECRET or not hmac.compare_digest(presented, SHARED_SECRET):
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
                if k.lower() not in ("host", "content-length", "x-api-key")
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
