#!/usr/bin/env bash
# cloud-init for the Aetherline k3s node.
#
# Terraform's templatefile() interpolates dollar-brace sequences, so use the
# $(...) form for anything shell needs to expand.
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl ca-certificates

# Install k3s.
#
#   --disable traefik : k3s bundles Traefik, whose ServiceLB binds node ports
#                       80/443. The api-gateway Service in k8s/base is
#                       type LoadBalancer, which k3s's own ServiceLB (klipper)
#                       publishes on the node's IP. Leaving Traefik enabled
#                       makes the two fight over port 80. We keep servicelb
#                       and drop Traefik: no AWS load balancer needed.
curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL="${k3s_channel}" sh -s - --disable traefik

# Wait for the API server to actually answer before declaring bootstrap done.
for _ in $(seq 1 60); do
  if /usr/local/bin/k3s kubectl get node >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

# Convenience copy for the ubuntu user (the CD workflow uses `sudo k3s kubectl`,
# which needs no kubeconfig at all — this is for manual debugging).
mkdir -p /home/ubuntu/.kube
cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config
chown -R ubuntu:ubuntu /home/ubuntu/.kube
chmod 600 /home/ubuntu/.kube/config

# k3s ships local-path-provisioner as the default StorageClass, which is what
# the Postgres and Kafka PVCs in k8s/base bind to. No EBS CSI driver needed.
{
  echo "=== Aetherline k3s bootstrap $(date -Is) ==="
  /usr/local/bin/k3s kubectl version
  /usr/local/bin/k3s kubectl get nodes -o wide
  /usr/local/bin/k3s kubectl get storageclass
} > /var/log/aetherline-bootstrap.log 2>&1
