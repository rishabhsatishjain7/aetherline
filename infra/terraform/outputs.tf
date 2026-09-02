output "ecr_repository_urls" {
  description = "Push images here, e.g. via `docker push <url>:<tag>`"
  value       = { for name, repo in aws_ecr_repository.service : name => repo.repository_url }
}
