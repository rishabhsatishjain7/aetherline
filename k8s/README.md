# Kubernetes manifests

Local-dev-scale manifests (single replica per Postgres, 2 replicas per app
service). Uses plain Kustomize (`k8s/base`), no Helm.

## Deployment status (as of this session)

Manifests were applied to a real local minikube cluster and validated
against an actual Kubernetes API server — not just `kubectl --dry-run`.

**What's confirmed working:**

- All 5 service images build successfully via `minikube image build`.
- 20 of 22 pods reached `Running` and stayed healthy for ~40 hours,
  correctly surviving and recovering from repeated host-level
  interruptions (Kubernetes restarting them as designed).
- **A real bug was found and fixed**: `kafka.yaml` still referenced the
  discontinued `bitnami/kafka:3.7` image (see the main README for
  background on why that image was replaced project-wide). Corrected to
  `apache/kafka:3.7.0` with the matching env var names; the corrected pod
  was confirmed pulling and starting successfully.

**What's not yet confirmed:**

- Full cluster stability — the host used for this session (2 CPU / 3GB
  allocated to minikube) hit repeated resource exhaustion running the
  complete 23-pod system, causing intermittent apiserver crashes unrelated
  to application code.
- An end-to-end smoke test through the Kubernetes-deployed `api-gateway`
  (the equivalent of the one already verified against Docker Compose).

Sustained verification is a reasonable follow-up on a host with more
headroom, or with resource requests/limits tuned down further for
constrained local dev environments.

## Running locally with minikube

```bash
minikube start
eval $(minikube docker-env)   # build images directly into minikube's registry

for svc in order-service inventory-service payment-service notification-service api-gateway; do
  docker build -t aetherline/$svc:latest ./services/$svc
done

kubectl apply -k k8s/base
kubectl -n aetherline get pods -w      # wait for everything to go Running

minikube service api-gateway -n aetherline   # opens the gateway in a browser
```

## Running locally with k3s / k3d

```bash
k3d cluster create aetherline
for svc in order-service inventory-service payment-service notification-service api-gateway; do
  docker build -t aetherline/$svc:latest ./services/$svc
  k3d image import aetherline/$svc:latest -c aetherline
done
kubectl apply -k k8s/base
```

## Notes / deliberate scope decisions

- **Kafka is a single-node StatefulSet.** Fine for local dev; a real
  deployment would use a managed service (MSK, Confluent Cloud) or the
  Strimzi operator for a multi-broker cluster with replication.
- **Postgres is a single-replica StatefulSet per service**, no
  replication/backup configured. A real deployment uses a managed DB (RDS)
  per service instead of self-hosting Postgres in the cluster.
- **HPA is only on `order-service` and `inventory-service`** — the two
  services on the synchronous request path (the API Gateway proxies to
  them directly). The consumers scale by adding replicas manually / via a
  KEDA Kafka-lag-based scaler in a fuller build; plain CPU-based HPA doesn't
  make as much sense for a consumer that's often idle between messages.
- **Secrets are placeholder values** committed in `secrets.yaml` for
  local-cluster convenience only. Never do this against a real cluster —
  use Sealed Secrets, External Secrets Operator, or your cloud's secret
  manager instead.
