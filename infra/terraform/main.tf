# Scope note: this stack provisions the ECR repositories plus a single EC2
# host running k3s, which stands in for a managed Kubernetes control plane.
#
# Why not EKS: the EKS control plane alone is ~$73/month before any nodes,
# plus ~$32/month for the NAT gateway a private-subnet node group needs.
# For a single-node demo the k3s host is real Kubernetes at ~$32/month
# total, and k8s/base stays the single source of truth. See ../README.md
# for the full tradeoff and how to move to EKS later.

resource "aws_ecr_repository" "service" {
  for_each             = toset(var.services)
  name                 = "aetherline-${each.value}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.common_tags
}

resource "aws_ecr_lifecycle_policy" "service" {
  for_each   = aws_ecr_repository.service
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last ${var.image_retention_count} images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.image_retention_count
      }
      action = { type = "expire" }
    }]
  })
}
