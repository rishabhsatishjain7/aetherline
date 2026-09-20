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
breakage. This is the gate; CD never runs unless CI passed.

`.github/workflows/cd.yml` — triggered by CI completing successfully on
`main` (or manually via `workflow_dispatch`): authenticates to AWS with
GitHub OIDC, builds one image per service tagged with the **commit SHA**,
pushes them to ECR, then deploys to the Kubernetes cluster over SSH and runs
a smoke test through the gateway. See "Deploying to AWS" below.

## Deploying to AWS

### Architecture

```
GitHub Actions (CI passes on main)
        │  OIDC  (no stored AWS keys)
        ▼
      AWS ── ECR (5 immutable SHA-tagged repos)
        │
        │  build & push
        ▼
   EC2 instance running k3s ── namespace "aetherline"
        ├─ api-gateway      (LoadBalancer → k3s ServiceLB on the node's EIP:80)
        ├─ order / inventory / payment / notification services + consumers
        ├─ Kafka (single-node StatefulSet)
        └─ 4 × Postgres (StatefulSet each, 1Gi local-path PVC)
```

The application is unchanged from the Compose/Kubernetes setup — the same
`k8s/base` manifests, with an AWS overlay in `k8s/overlays/aws` that pins
replicas to 1 and injects ECR pull credentials.

### AWS services used

| Service | Role | Cost |
|---|---|---|
| EC2 (t3.medium) | runs the whole cluster via k3s | ~$30/month |
| EBS (20GB gp3) | root volume | ~$1.60/month |
| Elastic IP | stable address for the node | free while attached |
| ECR | 5 repositories, SHA-tagged images | pennies |
| IAM + OIDC | GitHub Actions federation | free |
| VPC / subnet / IGW / security group | networking, **no NAT gateway** | free |

**~$32/month total.** See `infra/terraform/README.md` for why this is k3s on
EC2 rather than EKS (~$120+/month for the same result at this scale).

### Prerequisites

- An AWS account with billing activated
- Terraform >= 1.7
- AWS credentials for you, the human running `terraform apply`
- Admin rights on this GitHub repo (to add variables/secrets)

### 1. Provision AWS infrastructure

```bash
cd infra/terraform
cp example.tfvars terraform.tfvars      # optional: narrow the SSH CIDR
terraform init
terraform apply
```

This creates the ECR repos, VPC, security group, k3s node (with cloud-init
installing k3s), Elastic IP, and the GitHub OIDC provider + deploy role.

Note the outputs — you need them in the next step:

```bash
terraform output
```

> If your account **already has** the GitHub OIDC provider, `apply` fails with
> `EntityAlreadyExists`. Re-run with `create_oidc_provider = false` and
> `existing_oidc_provider_arn = "arn:aws:iam::<id>:oidc-provider/token.actions.githubusercontent.com"`.

### 2. Point GitHub at it

Settings → Secrets and variables → Actions. **Variables** (not secrets):

| Variable | Value |
|---|---|
| `AWS_REGION` | your region, e.g. `ap-south-1` |
| `AWS_ROLE_TO_ASSUME` | `terraform output -raw github_actions_role_arn` |
| `AWS_ECR_REGISTRY` | `terraform output -raw ecr_registry` |
| `AWS_DEPLOY_HOST` | `terraform output -raw deploy_host` |
| `AWS_DEPLOY_USER` | `terraform output -raw deploy_user` (`ubuntu`) |

And one **secret**:

| Secret | Value |
|---|---|
| `AWS_DEPLOY_SSH_KEY` | the private key Terraform generated (`aetherline-k3s.pem`), or the key matching your own `ssh_public_key` |

No `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` is used anywhere — the CD
workflow federates through OIDC and the deploy role is scoped to pushing and
pulling this project's ECR repositories only.

If any of these are missing, the CD `preflight` job fails immediately and
lists exactly which ones to set.

### 3. Deploy

Push to `main` (or run the CD workflow manually from the Actions tab). CI runs
first; if it is green, CD takes over:

1. **preflight** — checks the six config values above exist, resolves the
   commit SHA that CI actually validated.
2. **build-and-push** (matrix, 5 services) — assumes the OIDC role, logs in to
   ECR, builds `aetherline-<service>:<sha>` and pushes it. Only the SHA tag is
   pushed: the repositories are `IMMUTABLE`, so a `latest` tag would be
   rejected on the second deploy.
3. **deploy** — SSHes to the node, waits for the k3s API, refreshes the ECR
   pull secret (tokens expire after 12h), rewrites the overlay's image
   references to `<registry>/aetherline-<service>:<sha>`, applies the
   manifests, waits for the Kafka/Postgres StatefulSets and all nine
   Deployments to roll out, then runs a smoke test.

The smoke test seeds a SKU and places an order **through the public gateway**,
then polls until the order reaches `CONFIRMED` — which only happens if the
Kafka producer/consumer path works end to end. A `kubectl apply` that returns
0 is not treated as success; the run fails unless the order actually resolves.

### Checking deployment status

```bash
# GitHub Actions
gh run list --workflow=CD --limit 5

# cluster — over SSH (the API server is not exposed publicly)
ssh -i aetherline-k3s.pem ubuntu@<deploy_host> 'sudo k3s kubectl -n aetherline get pods -o wide'
ssh -i aetherline-k3s.pem ubuntu@<deploy_host> 'sudo k3s kubectl -n aetherline get svc'
ssh -i aetherline-k3s.pem ubuntu@<deploy_host> 'sudo k3s kubectl -n aetherline get events --sort-by=.lastTimestamp | tail -20'

# the app
curl http://<deploy_host>/health
curl -X POST http://<deploy_host>/products -H 'Content-Type: application/json' \
  -d '{"sku":"WIDGET-1","name":"Widget","quantity_available":50}'
curl -X POST http://<deploy_host>/orders -H 'Content-Type: application/json' \
  -d '{"items":[{"sku":"WIDGET-1","quantity":2}]}'
```

Every image reference carries the commit SHA, so `kubectl -n aetherline get
deployments -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.template.spec.containers[0].image}{"\n"}{end}'`
tells you exactly what is running.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| CD `preflight` fails listing variables | the GitHub variables/secrets in step 2 are not set |
| `Could not assume role` / OIDC error | `AWS_ROLE_TO_ASSUME` wrong, or the repo/branch is not in the role's trust policy (`github_subject_claims`) |
| `ImagePullBackOff` | the ECR pull secret is stale, or the node cannot reach ECR — check egress and re-run CD |
| Pods `Pending` | node too small — check `kubectl describe node` for insufficient CPU/memory; use `t3.large` |
| `CrashLoopBackOff` on services | Postgres/Kafka not ready yet; check the StatefulSet pods first |
| Gateway unreachable from the internet | security group port 80, or `allowed_http_cidr` excludes the client |
| `EntityAlreadyExists` on `terraform apply` | the account already has the GitHub OIDC provider — see the note in step 1 |
| k3s never becomes ready | `ssh` in and read `/var/log/aetherline-bootstrap.log` and `journalctl -u k3s -n 100` |

### Tearing it down

```bash
cd infra/terraform && terraform destroy
```

Removes the instance, EBS volume, EIP, VPC, security group, IAM role, OIDC
provider and **all five ECR repositories with their images**. To pause rather
than delete, `aws ec2 stop-instances` — the EIP then incurs a small charge.



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
- Auth on the API Gateway is a **shared-secret gate only** (`X-API-Key` vs
  `API_GATEWAY_SHARED_SECRET`, `/health` exempt, fails open when the env var is
  unset — see `services/api-gateway/app/main.py` and the deployment section).
  That is demo-appropriate, not user authentication: no login, no per-user
  identity, no JWT, no rotation, no audit trail. JWT was on the original plan
  and is still the right next step before treating this as a real multi-user,
  internet-facing service.
- Consumers scale by replica count only (no Kafka-lag-based autoscaling
  like KEDA) — see `k8s/README.md`.
