output "ecr_repository_urls" {
  description = "Push images here, e.g. via `docker push <url>:<tag>`"
  value       = { for name, repo in aws_ecr_repository.service : name => repo.repository_url }
}

output "ecr_registry" {
  description = "ECR registry hostname — set this as the AWS_ECR_REGISTRY GitHub variable"
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}

output "deploy_host" {
  description = "Public IP of the k3s node — set this as the AWS_DEPLOY_HOST GitHub variable"
  value       = aws_eip.node.public_ip
}

output "deploy_user" {
  description = "SSH user for the k3s node — set this as the AWS_DEPLOY_USER GitHub variable"
  value       = "ubuntu"
}

output "github_actions_role_arn" {
  description = "IAM role for GitHub Actions to assume via OIDC — set this as the AWS_ROLE_TO_ASSUME GitHub variable"
  value       = aws_iam_role.github_deploy.arn
}

output "ssh_command" {
  description = "Convenience SSH command for the node"
  value       = "ssh -i <private-key> ubuntu@${aws_eip.node.public_ip}"
}

output "generated_private_key_path" {
  description = "Path to the generated private key, if Terraform created the keypair (else empty)"
  value       = try(local_sensitive_file.private_key[0].filename, "")
}

output "kubectl_hint" {
  description = "Run kubectl against the deployed cluster over SSH"
  value       = "ssh -i <private-key> ubuntu@${aws_eip.node.public_ip} 'sudo k3s kubectl get pods -n aetherline'"
}
