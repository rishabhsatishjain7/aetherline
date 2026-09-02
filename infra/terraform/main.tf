# Scope note: this provisions ONLY the ECR repositories. The EKS cluster,
# VPC, and node groups are assumed to already exist (typically owned by a
# separate platform/infra repo in a real org, not re-provisioned per
# application). Wiring this up to an existing cluster is just pointing
# .github/workflows/cd.yml's EKS_CLUSTER_NAME secret at it.

resource "aws_ecr_repository" "service" {
  for_each             = toset(var.services)
  name                 = "aetherline-${each.value}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
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
