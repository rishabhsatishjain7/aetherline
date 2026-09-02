# Aetherline — Distributed Order Processing Platform

Event-driven microservices platform: 5 services, Kafka, Postgres
(database-per-service), Kubernetes, CI/CD to AWS, Prometheus/Grafana, and a
Locust load test. Built in four stages; all four are in this repo.

## Architecture

```
                              ┌──────────────┐
                    ┌────────▶│  Notification │ (consumes: orders.confirmed,
                    │         │    Service     │  orders.failed, inventory.released)
                    │         └──────────────┘
   Client                                              order-db   inventory-db
     │                                                     │            │
     ▼                                                     ▼            ▼
┌──────────┐    orders.placed     ┌───────────┐  reserve  ┌────────────────┐
│   API    │──────────────────▶  │  Order    │◀─────────▶│   Inventory     │
│ Gateway  │                      │ Service   │           │   Service       │
└──────────┘                      └───────────┘           └────────────────┘
                                        ▲                          │
                              orders.cancelled ◀──── inventory.reserved
                          payments.succeeded/         inventory.failed
                          payments.failed                    │
                                        │                     ▼
                                        │              ┌──────────────┐
                                        └──────────────│   Payment     │
                                       inventory.reserved│  Service    │
                                                         └──────────────┘
                                                          payment-db
```

All arrows between services are Kafka topics, not direct calls — the only
synchronous HTTP call in the whole system is Client → API Gateway →
Order/Inventory Service for the initial request/response. Everything past
that point is asynchronous.

## Kafka topics

| Topic | Producer | Consumer(s) |
|---|---|---|
| `orders.placed` | order-service (outbox) | inventory-service |
| `orders.cancelled` | order-service (outbox) | inventory-service |
| `inventory.reserved` | inventory-service (outbox) | order-service, payment-service |
| `inventory.failed` | inventory-service (outbox) | order-service |
| `inventory.released` | inventory-service (outbox) | notification-service |
| `payments.succeeded` | payment-service (outbox) | order-service |
| `payments.failed` | payment-service (outbox) | order-service |
| `orders.confirmed` | order-service (outbox) | notification-service |
| `orders.failed` | order-service (outbox) | notification-service |

Every producer writes via a **transactional outbox** (an `outbox_events`
table written in the same DB transaction as the state change, relayed to
Kafka by a background poller) rather than publishing directly from a
request/message handler — so a crash between "state changed" and "event
published" can't happen. Every consumer is **idempotent** against its own
DB state (checked via a reservation/payment/notification row keyed by
order_id), because Kafka delivery here is at-least-once and messages can be
redelivered.

Consumer offsets are committed manually, after a message is fully handled
(not on Kafka's auto-commit timer) — so a commit interval landing mid-retry
can't advance the offset past a message that hasn't actually been durably
processed yet. And the idempotency checks are atomic, not check-then-act: a
`SELECT ... exists?` followed by a write has a race window where two
consumer replicas (every consumer runs 2 pods — see `k8s/`) can both pass
the check before either commits. `inventory-service` closes this with a
`ProcessedOrderClaim` row whose primary key is the order_id, inserted and
committed *before* any stock is touched; `payment-service` and
`notification-service` rely on their own tables' primary/unique key
constraints the same way, catching `IntegrityError` as the "someone else
already handled this" signal rather than crashing. See
`tests/test_concurrency.py` in each service for a test that actually
exercises two threads racing the same order_id.

## Services

| Service | Role | Has own DB | Has Kafka consumer worker |
|---|---|---|---|
| `api-gateway` | Single public entry point; thin reverse proxy | No | No |
| `order-service` | Order lifecycle, saga orchestration via events | Yes (orders) | Yes |
| `inventory-service` | Stock reservation, row-locked to prevent overselling | Yes (inventory) | Yes |
| `payment-service` | Mock payment processing | Yes (payments) | Yes |
| `notification-service` | Terminal consumer; records "sent" notifications | Yes (notifications) | Yes |

### Order lifecycle
`PENDING → CONFIRMED` (inventory reserved AND payment succeeded), or
`PENDING → FAILED` (inventory couldn't be reserved, or payment declined
after inventory *was* reserved — in which case a compensating
`orders.cancelled` event releases the stock). `POST /orders` now returns
**202 Accepted** with the order `PENDING` — the outcome resolves
asynchronously. Poll `GET /orders/{id}` or `GET /orders/{id}/events`, or
consume `orders.confirmed`/`orders.failed` directly if you're another
service.

### Idempotency
`POST /orders` still accepts `Idempotency-Key`. A retry with the same key
returns the existing order rather than placing a duplicate.

## Running it locally (Docker Compose)

```bash
docker-compose up --build
```

Brings up: Kafka (KRaft, single node), 4 Postgres instances, all 5 services
(each with an API container and, where applicable, a separate consumer
container), Prometheus, and Grafana.

| Component | URL |
|---|---|
| API Gateway (use this) | http://localhost:8000 |
| Order Service (direct/debug) | http://localhost:8010 |
| Inventory Service (direct/debug) | http://localhost:8011 |
| Payment Service (direct/debug) | http://localhost:8012 |
| Notification Service (direct/debug) | http://localhost:8013 |
| Prometheus | http://localhost:9090 |
| Grafana (admin/admin) | http://localhost:3000 |

### Manual walkthrough

```bash
# 1. Seed stock
curl -X POST localhost:8011/products \
  -H "Content-Type: application/json" \
  -d '{"sku": "WIDGET-1", "name": "Widget", "quantity_available": 5}'

# 2. Place an order through the gateway -- 202, PENDING
curl -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"items": [{"sku": "WIDGET-1", "quantity": 2}]}'

# 3. Poll until it resolves (should become CONFIRMED within ~1-2s as the
#    events flow through Kafka)
curl localhost:8000/orders/<order_id>

# 4. Check the audit trail
curl localhost:8000/orders/<order_id>/events

# 5. Check the notification was recorded
curl localhost:8013/notifications/<order_id>

# 6. Try a payment failure: set PAYMENT_FAILURE_RATE=1.0 on payment-consumer
#    in docker-compose.yml, restart it, place another order -- watch it end
#    up FAILED and stock get released back.
```

## Running the tests

Each service has its own pytest suite (no Kafka/Docker needed — DB-layer
and event-handler logic is tested directly against a temp SQLite DB,
simulating Kafka message delivery by calling the same functions the
consumers call).

```bash
cd services/<service-name>
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

`services/inventory-service/tests/test_concurrency.py` is worth reading
specifically — it proves the "prevents overselling under concurrent load"
claim with two threads racing for the last unit of stock.

## Kubernetes

See [`k8s/README.md`](k8s/README.md). Kustomize manifests for all services,
Kafka, and Postgres, with a `HorizontalPodAutoscaler` on the two services
on the synchronous request path (order-service, inventory-service).

## CI/CD

`.github/workflows/ci.yml` — lints and runs every service's test suite on
every push/PR, then does a build-only Docker build to catch Dockerfile
breakage.

`.github/workflows/cd.yml` — on push to `main`: builds and pushes images to
ECR (OIDC-federated, no stored AWS keys), then updates the EKS deployment
images and applies the Kustomize manifests.

`infra/terraform/` — provisions the ECR repositories only. The EKS cluster
itself is assumed to already exist (see `infra/terraform/README.md` for why
that's out of scope here).

## Observability

Every FastAPI service exposes Prometheus metrics at `/metrics`
(`prometheus-fastapi-instrumentator`). `observability/prometheus.yml`
scrapes all five. Grafana comes pre-provisioned with the Prometheus
datasource — build a dashboard, or import the community FastAPI
Instrumentator dashboard (Grafana.com ID 14282) as a starting point.

## Load testing

```bash
pip install -r load-test/requirements.txt
locust -f load-test/locustfile.py --host http://localhost:8000
```

Open http://localhost:8089, set concurrent users/spawn rate, and run
against the gateway. **The throughput/latency numbers on the resume bullet
need to come from actually running this** against a real deployment (ideally
the Kubernetes one, not Compose) — nothing in this repo hardcodes or fakes
those numbers.

## What's deliberately out of scope

- Kafka and Postgres run single-node/single-replica everywhere. Production
  would use managed services (MSK/Confluent, RDS) instead of self-hosting
  either in the cluster.
- No auth/authZ on the API Gateway yet (JWT was on the original plan but
  didn't make it into this pass — worth adding before treating this as
  internet-facing).
- Consumers scale by replica count only (no Kafka-lag-based autoscaling
  like KEDA) — see `k8s/README.md`.
