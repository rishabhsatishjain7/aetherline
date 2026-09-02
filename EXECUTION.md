# Execution Runbook

Follow this in order. Each stage has a "confirm before moving on" check —
don't skip ahead if a check fails; fix it there, since later stages assume
earlier ones actually work.

## Prerequisites

- Docker + Docker Compose v2 (`docker compose version`)
- Python 3.12+ (for running tests without Docker)
- Git, and a GitHub account
- Optional, only needed for the Kubernetes/AWS stages: `kubectl`,
  `minikube` or `k3d`, `terraform`, AWS CLI

---

## Stage 1 — Unit tests, no Docker needed

Fastest feedback loop. Run this first — if something's broken at this
level, Docker will just make it slower to find.

```bash
cd aetherline
for svc in order-service inventory-service payment-service notification-service api-gateway; do
  echo "=== $svc ==="
  (cd services/$svc && \
   python3 -m venv .venv && source .venv/bin/activate && \
   pip install -q -r requirements.txt -r requirements-dev.txt && \
   pytest -v --timeout=30 && \
   deactivate)
done
```

**Confirm before moving on:** all 5 services report passing tests. If one
fails, read the traceback before touching Docker — a test failure here is
almost always a real bug, not an environment issue.

---

## Stage 2 — Docker Compose (the full system, locally)

```bash
docker compose up --build
```

This builds all 9 images and starts Kafka, 4 Postgres instances, 5 API
containers, 4 consumer containers, Prometheus, and Grafana. First build
will take a few minutes (installing dependencies in 9 separate images).

**Watch the logs as it starts, specifically for:**
- `kafka` reaching healthy (other services wait on this)
- Each `*-consumer` container — these are the least-tested part of this
  system. If one crash-loops, `docker compose logs <name>-consumer` and
  read the traceback. Common failure modes to expect: a Kafka connection
  refused before the broker is fully up (the retry logic in
  `kafka_utils.py` should absorb this — if it doesn't, that's a real bug to
  fix), or a `ModuleNotFoundError` if a dependency got missed somewhere.

**Confirm before moving on:**

```bash
docker compose ps   # everything should show "running" or "healthy", nothing "restarting"
curl localhost:8000/health   # api-gateway
curl localhost:8010/health   # order-service
curl localhost:8011/health   # inventory-service
curl localhost:8012/health   # payment-service
curl localhost:8013/health   # notification-service
```

All five should return `{"status":"ok",...}`. If any container is stuck
restarting, stop here and fix it — don't proceed to the smoke test against
a partially-up system.

---

## Stage 3 — Manual smoke test (proves the event flow actually works)

```bash
# 1. Seed stock directly against inventory-service
curl -X POST localhost:8011/products \
  -H "Content-Type: application/json" \
  -d '{"sku": "WIDGET-1", "name": "Widget", "quantity_available": 5}'

# 2. Place an order through the gateway
ORDER_ID=$(curl -s -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"items": [{"sku": "WIDGET-1", "quantity": 2}]}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Order: $ORDER_ID"

# 3. Poll until it resolves -- should flip PENDING -> CONFIRMED within a
#    couple seconds as the Kafka events flow through all 3 services
sleep 3
curl localhost:8000/orders/$ORDER_ID

# 4. Check the full audit trail
curl localhost:8000/orders/$ORDER_ID/events

# 5. Check inventory actually decremented
curl localhost:8011/products/WIDGET-1

# 6. Check notification-service recorded the confirmation
curl localhost:8013/notifications/$ORDER_ID
```

**Confirm before moving on:** step 3 shows `"status":"CONFIRMED"`, step 5
shows `quantity_available: 3`, step 6 shows an `ORDER_CONFIRMED`
notification. This is the first time this exact flow — gateway → order
→ Kafka → inventory → Kafka → payment → Kafka → order → Kafka →
notification — has ever actually executed. If it doesn't resolve, check
each consumer's logs in the order the event should have traveled.

**Also worth trying:** place an order for `quantity: 100` (more than
stock) and confirm it resolves to `FAILED` with a `RESERVATION_FAILED`
audit entry, and that stock is untouched.

---

## Stage 4 — Load test (optional but proves the resume bullet)

```bash
pip install -r load-test/requirements.txt
locust -f load-test/locustfile.py --host http://localhost:8000
```

Open http://localhost:8089, set users/spawn rate, run for a few minutes.
**Write down the actual req/sec and p99 latency you observe** — that's
what belongs in the resume bullet, replacing the placeholder numbers.

When done: `docker compose down -v` (the `-v` also drops the DB volumes,
useful for a clean slate — omit it if you want to keep the data).

---

## Stage 5 — Kubernetes (optional, if you want to validate that layer too)

```bash
minikube start
eval $(minikube docker-env)

for svc in order-service inventory-service payment-service notification-service api-gateway; do
  docker build -t aetherline/$svc:latest ./services/$svc
done

kubectl apply -k k8s/base
kubectl -n aetherline get pods -w   # wait for everything Running/Ready
```

**Confirm before moving on:**

```bash
kubectl -n aetherline get pods    # all Running, no CrashLoopBackOff
minikube service api-gateway -n aetherline   # opens it in a browser / gives you a URL
```

If a pod is `CrashLoopBackOff`, `kubectl -n aetherline logs <pod-name>
--previous` to see why it died. This is the layer I have the least
confidence in, since it's never run against a real cluster before now.

---

## Stage 6 — Push to GitHub

Only do this once Stage 2/3 (at minimum) are confirmed working — don't
push code you haven't seen run.

```bash
cd aetherline
git init
git add .
git commit -m "Aetherline: event-driven order processing platform"
```

Create the repo on GitHub (via the web UI, or `gh repo create aetherline
--private --source=. --remote=origin` if you have the `gh` CLI), then:

```bash
git remote add origin https://github.com/<your-username>/aetherline.git
git branch -M main
git push -u origin main
```

**Confirm:** check the repo's **Actions** tab on GitHub. The `CI` workflow
(`.github/workflows/ci.yml`) should trigger automatically and run — lint +
tests for all 4 services, then a build-only Docker build for all 5. This
is a second, independent confirmation that the tests pass, on a clean
environment that isn't your machine.

### About the CD workflow

`.github/workflows/cd.yml` will also trigger, but it **will fail** unless
you've set up:
- An AWS OIDC role (`AWS_ROLE_TO_ASSUME` repo secret)
- `AWS_REGION` and `EKS_CLUSTER_NAME` repo secrets
- The ECR repos actually provisioned (`cd infra/terraform && terraform
  apply`)
- A real EKS cluster for `EKS_CLUSTER_NAME` to point at

If you don't have AWS infra set up yet, that's fine — a failing CD run
doesn't affect CI or block the push. Either set those secrets up before
pushing, or go to the repo's **Settings → Actions → Workflows** and
disable `cd.yml` until you're ready for it, so it doesn't just show a red
X for no reason on every push in the meantime.

---

## If something breaks and you're not sure where

Check in this order — it matches the direction data actually flows:
1. Is Kafka healthy? (`docker compose logs kafka`)
2. Did the producing service's consumer actually publish? (check that
   service's `*-consumer` logs for the outbox relay)
3. Did the consuming service receive it? (check its `*-consumer` logs)
4. Is the DB state what you expect? (`docker compose exec order-db psql -U
   aetherline -d orders -c "select * from orders;"`, similarly for other
   DBs)

Bring me whatever the logs say at the point it breaks.
