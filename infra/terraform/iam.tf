data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# GitHub Actions OIDC federation.
#
# The workflow assumes this role via sts:AssumeRoleWithWebIdentity using a
# short-lived token GitHub mints per run — no AWS_ACCESS_KEY_ID or
# AWS_SECRET_ACCESS_KEY is ever stored in the repo.
#
# If your account already has the GitHub OIDC provider (many do, it is
# account-wide and shared), set create_oidc_provider = false and pass the
# existing provider's ARN via existing_oidc_provider_arn.
# ---------------------------------------------------------------------------
locals {
  github_oidc_arn = (
    var.create_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : var.existing_oidc_provider_arn
  )
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = local.common_tags
}

data "aws_iam_policy_document" "github_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = var.github_subject_claims
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name_prefix        = "${var.project_name}-github-deploy-"
  description        = "Assumed by GitHub Actions via OIDC to push Aetherline images to ECR"
  assume_role_policy = data.aws_iam_policy_document.github_assume_role.json

  tags = local.common_tags
}

# Least privilege: only the authorisation token (which must be Resource "*")
# plus push/pull on this project's repositories. No EKS, no EC2, no IAM
# management — the CD workflow does not need them.
data "aws_iam_policy_document" "ecr_push_pull" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPushPullThisProject"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for repo in aws_ecr_repository.service : repo.arn]
  }
}

resource "aws_iam_role_policy" "ecr_push_pull" {
  name   = "${var.project_name}-ecr-push-pull"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.ecr_push_pull.json
}
