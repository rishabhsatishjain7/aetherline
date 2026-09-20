variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Prefix for AWS resource names"
  type        = string
  default     = "aetherline"
}

variable "services" {
  description = "Service names, one ECR repository is created per entry"
  type        = list(string)
  default     = ["order-service", "inventory-service", "payment-service", "notification-service", "api-gateway"]
}

variable "image_retention_count" {
  description = "How many tagged images to retain per repository before lifecycle policy expires older ones"
  type        = number
  default     = 10
}

# ---------------------------------------------------------------------------
# Cluster host (single EC2 running k3s) — deliberately cheaper than EKS.
#
# Size note: the app Deployments request 100m CPU / 128Mi each. The AWS
# overlay pins them to 1 replica each (9 pods => ~0.9 CPU / ~1.15GiB of
# requests), leaving room for k3s system pods and the in-cluster Kafka and
# Postgres StatefulSets.
#   t3.small  (2GB)  -> too small, pods go Pending / OOMKilled
#   t3.medium (4GB)  -> the default, comfortable
#   t3.large  (8GB)  -> use if you bump replicas back up or run the load test
# ---------------------------------------------------------------------------
variable "instance_type" {
  description = "EC2 instance type for the k3s node"
  type        = string
  default     = "t3.medium"
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size in GiB (holds the container images and local-path PVCs)"
  type        = number
  default     = 20
}

variable "ssh_public_key" {
  description = "SSH public key to authorise on the node. If empty, Terraform generates a keypair and writes the private key to ssh_private_key_path."
  type        = string
  default     = ""
}

variable "ssh_private_key_path" {
  description = "Where to write the generated private key (only used when ssh_public_key is empty)"
  type        = string
  default     = "./aetherline-k3s.pem"
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH (port 22) to the node. Narrow this to your own IP. Use 0.0.0.0/0 only for a throwaway demo."
  type        = string
  default     = "0.0.0.0/0"
}

variable "allowed_http_cidr" {
  description = "CIDR allowed to reach the api-gateway on port 80. Needs to be public for the CD smoke test (GitHub runners have dynamic IPs). WARNING: the gateway has no auth."
  type        = string
  default     = "0.0.0.0/0"
}

variable "k3s_channel" {
  description = "k3s release channel installed by cloud-init"
  type        = string
  default     = "stable"
}

# ---------------------------------------------------------------------------
# GitHub Actions OIDC
# ---------------------------------------------------------------------------
variable "github_subject_claims" {
  description = "Allowed OIDC `sub` claims for the deploy role. Tighten or extend as needed."
  type        = list(string)
  default     = ["repo:rishabhsatishjain7/aetherline:ref:refs/heads/main"]
}

variable "create_oidc_provider" {
  description = "Create the token.actions.githubusercontent.com OIDC provider. Set false if the AWS account already has one."
  type        = bool
  default     = true
}

variable "existing_oidc_provider_arn" {
  description = "ARN of an existing GitHub OIDC provider (only used when create_oidc_provider = false)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Extra tags applied to all resources"
  type        = map(string)
  default     = {}
}

