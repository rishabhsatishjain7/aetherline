"""
Aetherline API Gateway. Single public entry point: routes requests to the
internal services, which (aside from this gateway) are not exposed outside
the Docker/Kubernetes network. Deliberately thin -- no business logic here,
just routing, so it can't become a second place order/inventory rules live.
"""
import os
import httpx
from fastapi import FastAPI, Request, Response

from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Aetherline - API Gateway")
Instrumentator().instrument(app).expose(app)

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
            headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
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
