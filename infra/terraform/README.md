# Terraform — Aetherline AWS infrastructure

Provisions everything needed to run Aetherline on AWS:

| Resource | Purpose |
|---|---|
| 5 × ECR repositories | one per service, `IMMUTABLE` tags, scan-on-push, 10-image lifecycle |
| VPC + public subnet + IGW + route table | no NAT gateway (deliberately — see cost below) |
| Security group | SSH (22) and the api-gateway (80/443) |
| EC2 instance running **k3s** | the Kubernetes cluster |
| Elastic IP | stable address for `AWS_DEPLOY_HOST` |
| IAM OIDC provider + deploy role | GitHub Actions assumes this; no long-lived AWS keys |

## Why k3s on EC2 instead of EKS

The project is Kubernetes-native, so EKS is the "obvious" target — but for a
single-node demo it is hard to justify the price:

| | EKS | k3s on one EC2 (this) |
|---|---|---|
| Control plane | **~$73/month** | $0 |
| NAT gateway (needed for private-subnet nodes) | **~$32/month** | $0 (public subnet + IGW) |
| Worker node(s) | ~$15–30/month | ~$30/month (t3.medium) |
| Load balancer (optional) | ~$18/month | $0 (k3s ServiceLB) |
| **Approximate total** | **~$120+/month** | **~$32/month** |

k3s is real Kubernetes with a certified API server, so `k8s/base` remains the
single source of truth and nothing about the manifests is k3s-specific. Moving
to EKS later is a matter of pointing the CD workflow at a cluster — the
manifests and image flow do not change.

**Why `t3.medium` and not something smaller:** the 9 application Deployments
request 100m CPU / 128Mi each. The AWS overlay pins them to 1 replica each
(~0.9 CPU / ~1.15GiB of requests). On `t3.small` (2GB) the pods go `Pending`
or get OOMKilled once k3s's own system pods and the in-cluster Kafka/Postgres
are counted. `t3.medium` (4GB) fits with headroom; `t3.large` is the step up
if you raise replicas or run the load test against it.

## Network exposure & authentication

| Port | Default | Why |
|---|---|---|
| SSH (22) | **closed by default** — `allowed_ssh_cidr` is a **required** variable with no default, and Terraform validation rejects `0.0.0.0/0` | You must state your own IP (e.g. `203.0.113.10/32`) explicitly; SSH can never be accidentally world-open. Find your IP: `curl -s https://checkip.amazonaws.com`. |
| HTTP (80 → api-gateway) | intentionally `0.0.0.0/0` | The CD smoke test runs on GitHub-hosted runners with dynamic IPs, so the gateway must be publicly reachable. |

HTTP being open is acceptable **only because** the gateway enforces a
shared-secret gate: every route except `/health` requires an `X-API-Key`
header matching `API_GATEWAY_SHARED_SECRET` (a GitHub secret that CD injects
into the cluster as the `api-gateway-shared-secret` Secret on every deploy).
Unauthenticated requests get `401`, and the CD smoke test asserts the gate is
actually active before running its authenticated order flow.

**Limitation, stated plainly:** that gate is demo-appropriate, not production
authentication — one shared secret, no login, no per-user identity, no JWT,
no rotation, no audit trail. A real production deployment would need proper
JWT-based auth per user.

## Prerequisites

- An AWS account with billing enabled
- Terraform >= 1.7
- AWS credentials for *you* (the human running `apply`) — the GitHub OIDC role
  is only for CI

## Usage

```bash
cd infra/terraform
cp example.tfvars terraform.tfvars   # optional; adjust CIDRs and instance type
terraform init
terraform plan
terraform apply
```

Terraform generates an SSH keypair unless you pass `ssh_public_key`, and writes
the private key to `ssh_private_key_path` (default `./aetherline-k3s.pem`,
gitignored). **Keep that file** — the CD workflow needs it as a GitHub secret.

Cloud-init installs k3s with `--disable traefik`: k3s bundles Traefik, whose
ServiceLB binds node ports 80/443, and it would fight the api-gateway
`LoadBalancer` Service (which k3s's own ServiceLB publishes on the node IP —
that is how the gateway becomes reachable without an AWS load balancer).

## Outputs → GitHub configuration

| Terraform output | GitHub Actions name | Kind |
|---|---|---|
| `ecr_registry` | `AWS_ECR_REGISTRY` | variable |
| `deploy_host` | `AWS_DEPLOY_HOST` | variable |
| `deploy_user` | `AWS_DEPLOY_USER` | variable |
| `github_actions_role_arn` | `AWS_ROLE_TO_ASSUME` | variable |
| — (the `.pem` file) | `AWS_DEPLOY_SSH_KEY` | **secret** |
| — (you generate it, e.g. `openssl rand -hex 32`) | `API_GATEWAY_SHARED_SECRET` | **secret** |
| `aws_region` (input) | `AWS_REGION` | variable |

See the root `README.md` for the full deployment walkthrough.

## Cost

Roughly **$32/month** running continuously: t3.medium on-demand (~$30) plus a
20GB gp3 root volume (~$1.60). ECR storage is pennies at this image count.
The Elastic IP is free while attached to a running instance (~$3.60/month if
you stop the instance and leave the EIP).

To stop paying without losing the setup, `aws ec2 stop-instances` the node —
you keep the EBS volume and the EIP (the EIP then costs a small amount). To
remove everything, see below.

## Destroy

```bash
cd infra/terraform
terraform destroy
```

This removes the EC2 instance, EBS volume, EIP, VPC, security group, IAM role,
the GitHub OIDC provider, and — importantly — **all five ECR repositories and
every image in them**, since they are managed by this stack.

If the OIDC provider is shared with other projects in the account, set
`create_oidc_provider = false` before `apply` so `destroy` does not remove it.

