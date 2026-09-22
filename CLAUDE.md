# Aetherline — Project Reference for AI Agents

## What this is

A distributed order-processing platform: 5 event-driven microservices
(Order, Inventory, Payment, Notification, API Gateway) built with
Python/FastAPI, communicating asynchronously via Apache Kafka using the
transactional outbox pattern. Each service owns its own PostgreSQL
database (database-per-service). Containerized with Docker, deployable to
Kubernetes (Kustomize), with GitHub Actions CI/CD, Prometheus/Grafana
observability, and Locust load testing.

Repo: github.com/rishabhsatishjain7/aetherline

## Architecture

- **api-gateway** — public entry point, thin reverse proxy to the other services; mandatory shared-secret gate (X-API-Key vs `API_GATEWAY_SHARED_SECRET`, fail closed) — see its section below
- **order-service** — creates orders, orchestrates the saga via Kafka events, has its own consumer for inventory/payment results
- **inventory-service** — reserves/releases stock, row-locked to prevent overselling
- **payment-service** — mock payment processing
- **notification-service** — terminal consumer, records notifications
- Each of order/inventory/payment/notification has a paired `*-consumer`
  container running `python -m app.consumer` (same image, different
  command) for its Kafka listener + outbox relayer

Kafka topics: `orders.placed`, `orders.cancelled`, `inventory.reserved`,
`inventory.failed`, `inventory.released`, `payments.succeeded`,
`payments.failed`, `orders.confirmed`, `orders.failed`.

## Running locally (Docker Compose)

```powershell
cd C:\Users\admin\Downloads\aetherline\aetherline
docker compose up -d
docker compose ps   # confirm all 16 containers Up/healthy before doing anything else
```

Note: api-gateway, payment-service, notification-service, and the 4
consumers are defined WITHOUT healthchecks by design — they legitimately
show plain "Up" rather than "(healthy)". That's expected config, not a
problem. Only the 4 DBs, Kafka, order-service, and inventory-service have
healthchecks configured.

**Always verify `docker compose ps` shows all 16 containers before running
tests or load tests.** Missing consumers or an unhealthy Kafka means
results will be meaningless (orders will sit in PENDING forever).

## API gateway shared-secret gate (mandatory — fails closed)

The api-gateway checks an `X-API-Key` header against the env var
`API_GATEWAY_SHARED_SECRET` on every route except `/health`
(`services/api-gateway/app/main.py`), and **auth is always required — there is
no unauthenticated mode.**

- **Fail closed, deliberately: a misconfiguration stops the app from starting.**
  If `API_GATEWAY_SHARED_SECRET` is unset or blank the module raises at import
  time, so `uvicorn` exits and the container never comes up. A bad config is a
  loud boot failure, never a silent loss of authentication — there is no code
  path in which the gate becomes a no-op. (It used to fail open when unset;
  that behaviour was removed on purpose and is covered by
  `test_app_refuses_to_start_when_shared_secret_is_missing_or_empty`.)
- **Local dev uses an explicit non-secret placeholder, not an auth bypass.**
  `docker-compose.yml` sets `API_GATEWAY_SHARED_SECRET:
  local-dev-only-not-a-real-secret` so `docker compose up` stays zero-setup.
  That value is intentionally not sensitive and must never be reused anywhere
  real — but the gate is fully active locally, so every gateway call (curl,
  Locust) has to send the header. The test suite sets its own secret the same
  way, in `tests/conftest.py`.
- **The AWS deployment sets a real secret.** `k8s/overlays/aws/gateway-auth.yaml`
  wires the gateway's env to the `api-gateway-shared-secret` Secret (not
  committed — the CD workflow creates/rotates it from the
  `API_GATEWAY_SHARED_SECRET` GitHub secret on every deploy, and rejects an
  empty value before applying). The CD smoke test first asserts the gate is
  actually active (an unauthenticated call must NOT return 200) and then
  authenticates all of its calls.
- **The gateway never forwards the secret upstream** — the proxy strips
  `X-API-Key` before calling internal services.
- **`/metrics` is gated too** — the local Prometheus scrape in
  `observability/prometheus.yml` sends the dev placeholder header.
- **Limitation, stated plainly: this is a shared-secret gate appropriate for a
  demo/capstone deployment, NOT full user authentication.** No login, no
  per-user identity, no JWT, no expiry/rotation, no audit trail — anyone
  holding the one secret can do anything. A real production deployment would
  need proper JWT-based auth per user.

## Running tests (per service, from project root)

```powershell
cd services\<service-name>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
ruff check app   # lint must also be clean — CI checks this
```

Expected: order-service 13 passed, inventory-service 13 passed,
payment-service 6 passed, notification-service 6 passed, api-gateway 17
passed (55 total; 8 of the gateway's 17 are the shared-secret-gate tests,
including the three fail-closed "refuses to start" cases).
All 5 services must also pass `ruff check app` with zero errors.

## Load testing

```powershell
cd load-test
.\.venv\Scripts\Activate.ps1
locust -f locustfile.py --host http://localhost:8000 --headless -u 20 -r 2 -t 3m --csv=results
```

Note: every gateway call needs `X-API-Key` (auth is mandatory and fails
closed). `locustfile.py` sends the local-dev placeholder from
`docker-compose.yml` by default — set `API_GATEWAY_SHARED_SECRET` to the real
secret when load-testing a deployment, or every request comes back 401.

**Methodology note (read before comparing any future run to a past one):**
A run's total request count depends on both duration AND how fast the
server responds — it is NOT a fixed number, so never compare "total
requests" across runs with different durations or host load. Only compare
RPS, and percentile latencies, and only when both runs used the same
-u/-r/-t flags and a broadly similar host load state. Always record the
exact command, run duration, and a `docker stats` snapshot alongside any
results you intend to treat as a baseline going forward. Local artifacts
(`results_*.csv`, `probe_*`, `smoke_*`) are gitignored — do not commit
raw run artifacts to the repo; if a specific result is worth keeping as
evidence, copy it into a dated file under `load-test/evidence/` and commit
that intentionally instead.

## Known issues already fixed (do not reintroduce)

- **`bitnami/kafka` is discontinued** (Bitnami paywalled it in 2025). Must
  use `apache/kafka:3.7.0` with `KAFKA_*` env var names (not Bitnami's
  `KAFKA_CFG_*`). This applies in BOTH `docker-compose.yml` and
  `k8s/base/kafka.yaml` — check both if touching Kafka config.
- **`kafka-python==2.0.2` is broken on Python 3.12** (vendored `six`
  module bug). Must be `kafka-python==3.0.10`, and the exception import
  must be `from kafka.errors import KafkaError` (not the removed
  `NoBrokersAvailable`). This affects `app/kafka_utils.py` in all 4
  services that use Kafka.
- **SQLAlchemy default connection pool (size=5) bottlenecks under
  concurrent load.** Every service's `app/database.py` sets
  `pool_size=20, max_overflow=20, pool_timeout=30` explicitly on
  `create_engine()`. Verify with:
```powershell
  docker exec aetherline-<service>-1 python -c "from app.database import engine; print(engine.pool.size(), engine.pool._max_overflow)"
```
  (should print `20 20`)
- **Outbox relayer polling needed an index.** `outbox_events` has a
  composite index `ix_outbox_events_published_id ON (published, id)` —
  `published` is the leading column so the relayer's polling query is
  covered.
- **Consumers crash if `models.Base.metadata.create_all()` isn't called
  in `app/consumer.py`**, not just `app/main.py` — the consumer is a
  separate process and doesn't share the API's startup code.
- **Idempotency must be atomic, not check-then-act.** Order-level
  dedup uses a `ProcessedOrderClaim` table (unique constraint on
  order_id) — inserting and catching `IntegrityError` on conflict, not a
  SELECT-then-INSERT pattern.
- **The api-gateway must never fail open on auth (security fix).** It used to
  skip the `X-API-Key` gate entirely whenever `API_GATEWAY_SHARED_SECRET` was
  unset, so a misconfiguration silently published a wide-open gateway. It now
  fails closed: a missing or blank secret raises at import time and the process
  never starts, and `/health` is the only unauthenticated route. Do not
  reintroduce any "no secret = no auth" path, and do not drop the placeholder
  from `docker-compose.yml` (the gateway needs it to boot locally).
- **CI/CD workflows trigger on `branches: [main]`** — if a push doesn't
  show up in the Actions tab, check you're actually on `main`, not
  `master`.
- **The CD workflow deploys to a single-node k3s host on EC2, not EKS** — see
  `infra/terraform/README.md` for the cost reasoning (~$32/month vs
  ~$120+/month). It fails at its `preflight` job until the six GitHub
  variables/secrets listed in the root README ("Deploying to AWS" → step 2)
  are configured. That failure names exactly what is missing, so it is a
  setup signal, not a regression — and CI stays green independently of it.

## Windows/PowerShell-specific gotchas

- Multi-line heredoc pastes (`@' ... '@`) into PowerShell can crash
  PSReadLine on this environment. Prefer single-line commands or `-replace`
  patches over large multi-line file rewrites. Verify the resulting file
  with `Get-Content` afterward — don't trust the "success" message alone.
- `.venv\Scripts\Activate.ps1` sometimes needs to be `.\.venv\Scripts\Activate.ps1`
  (leading `.\`) or PowerShell misparses it as a module name.
- If a `.venv` throws "No Python at ...", the interpreter it was created
  with no longer exists — delete and recreate the venv, don't try to repair it.
- Watch disk space (`Get-Volume -DriveLetter C`) — Docker's WSL2 virtual
  disk grows every build and doesn't shrink on its own. If free space
  drops under ~2GB, run `docker system prune -a` then compact the vhdx
  via `Optimize-VHD` in an admin PowerShell window before continuing.
- If Docker Desktop hangs/won't start, check for duplicate zombie
  processes first (`Get-Process | Where-Object { $_.Name -like "*docker*" }`)
  before assuming a full reboot is needed.

## Kubernetes status (honest, don't overstate)

Manifests in `k8s/base/` are written, apply cleanly, and were validated
against a real minikube cluster — a real bug was found and fixed there
(the same bitnami/kafka issue). Full sustained stability was NOT achieved
on a 2 CPU / 3GB local host running 23 pods; treat K8s as "validated, not
production-stable on this hardware" per `k8s/README.md`.

## AWS deployment (added later — see README "Deploying to AWS")

- **Shape:** GitHub Actions → (OIDC) → ECR → a **single EC2 instance running
  k3s** → `k8s/base` + `k8s/overlays/aws`. No EKS, no NAT gateway, no ALB —
  that keeps it at ~$32/month instead of ~$120+/month. k3s is real Kubernetes,
  so `k8s/base` stays the source of truth.
- **Terraform** (`infra/terraform/`) now provisions ECR ×5, a public-only VPC,
  an EC2 k3s node with cloud-init, an Elastic IP, the GitHub OIDC provider and
  a least-privilege deploy role. Validated with `terraform fmt -check` and
  `terraform validate`; `plan`/`apply` need real AWS credentials.
- **Images are tagged with the commit SHA and never `latest`.** The ECR repos
  are `IMMUTABLE`, so pushing `latest` on a second deploy would be rejected
  outright — that was one of the original CD bugs.
- **k3s is installed with `--disable traefik`.** Traefik's ServiceLB binds node
  ports 80/443 and fights the api-gateway `LoadBalancer` Service, which k3s's
  own ServiceLB publishes on the node's IP (that is what makes the gateway
  reachable without paying for an AWS load balancer).
- **The node needs >= t3.medium (4GB).** The nine app Deployments request
  ~1.8 CPU / ~2.25GiB at the base's 2 replicas, which does not fit on a 2 vCPU
  node — hence the AWS overlay pins them to 1 replica each.
- **AWS credentials are OIDC-federated.** No long-lived AWS keys exist in the
  repo or in GitHub secrets; the only SSH secret is the k3s node key, which the
  deploy job uses to run `sudo k3s kubectl` on the box.
- **`*.pem`, `*.tfstate`, `.terraform/` and `*.tfvars` are gitignored**, but
  `.terraform.lock.hcl` **is** committed on purpose (provider pinning).

## Known issues fixed / operational findings

### Already fixed (verified live this session)

- **SQLAlchemy pool exhaustion** — `pool_size` 5 → 20 (+`max_overflow=20`) in all
  four services; confirmed by reading the live engine inside the container.
- **order-consumer crash-loop** — `consumer.commit()` was unguarded; a single
  `KafkaTimeoutError` raised out of `run_consumer_loop` and killed the process
  *and* the in-process outbox relayer. Now wrapped in
  `try/except (KafkaTimeoutError, KafkaError)`.
- **No consumer restart policy** — consumers now carry `restart: on-failure`, so
  an escaped crash self-recovers.
- **Outbox seq-scan** — the 1s relayer poll full-scanned `outbox_events`; now
  covered by `ix_outbox_events_published_id (published, id)`.
- **Pipeline stalls** — verified end-to-end this session: order flipped
  `PENDING → CONFIRMED` with `ORDER_CREATED → INVENTORY_RESERVED →
  ORDER_CONFIRMED`.

### Operational findings (found the hard way — read before debugging)

- **The Docker VM has only 4 GB RAM (4 vCPUs).** Under sustained load it
  **OOM-kills containers** — observed as mass `Exited (137)` across DBs, Kafka,
  services and consumers. After an OOM kill, Postgres restarts and spends
  **100–150s+ replaying WAL / fsyncing its data directory** before it accepts
  connections again. Don't run a competing stack on this box.
- **Kafka and Postgres healthchecks flap under CPU contention.** `docker compose
  ps` shows `(unhealthy)` while the container is `Up` and actually fine: the
  Kafka healthcheck (`kafka-topics.sh --list`) exceeds its 10s timeout, and
  Postgres rejects probes during recovery.
  **Before assuming code breakage, check the DB log for
  `FATAL: the database system is in recovery mode`** (or `Consistent recovery
  state has not been yet reached`). If you see that, the failure is recovery,
  not your change — wait it out rather than restarting into the same state.
- **A lone `unhealthy` reading for Kafka right after `docker compose up` is
  often transient.** The Kafka healthcheck shells out to `kafka-topics.sh`,
  which spins up a JVM on every run — on this 4-CPU host that can exceed the
  10s timeout while the broker is actually fine. Re-check with
  `docker compose ps` before debugging, and don't wipe the `kafka-data` volume
  unless the logs show real corruption.