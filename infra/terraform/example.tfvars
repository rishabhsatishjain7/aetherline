# Copy to terraform.tfvars and adjust. Everything here is optional — the
# defaults deploy a t3.medium k3s node into ap-south-1 with the five ECR
# repositories.
#
# This file is committed as a template; terraform.tfvars is gitignored.

aws_region    = "ap-south-1"
project_name  = "aetherline"
instance_type = "t3.medium"

# allowed_ssh_cidr is REQUIRED — replace with your own IP (/32). Terraform
# rejects 0.0.0.0/0 for SSH by design. Find your IP: curl -s https://checkip.amazonaws.com
allowed_ssh_cidr = "203.0.113.10/32" # ← REPLACE — documentation IP, not a real one
# allowed_http_cidr stays public (0.0.0.0/0) so the CD smoke test (GitHub-hosted
# runners, dynamic IPs) can reach the gateway. Safe for a demo because the
# gateway enforces the shared-secret X-API-Key gate — see CLAUDE.md.
allowed_http_cidr = "0.0.0.0/0"

# Optional: bring your own keypair instead of letting Terraform generate one.
# ssh_public_key = "ssh-rsa AAAA... you@laptop"

# If your AWS account already has the GitHub Actions OIDC provider, avoid a
# duplicate-provider error:
# create_oidc_provider       = false
# existing_oidc_provider_arn = "arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com"
