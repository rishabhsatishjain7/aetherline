# Copy to terraform.tfvars and adjust. Everything here is optional — the
# defaults deploy a t3.medium k3s node into ap-south-1 with the five ECR
# repositories.
#
# This file is committed as a template; terraform.tfvars is gitignored.

aws_region    = "ap-south-1"
project_name  = "aetherline"
instance_type = "t3.medium"

# Narrow these before leaving the stack up. The SSH CIDR should ideally be
# your own IP (/32). allowed_http_cidr must stay public if you want the CD
# smoke test (which runs from GitHub-hosted runners) to reach the gateway —
# but note the gateway has no authentication.
allowed_ssh_cidr  = "0.0.0.0/0"
allowed_http_cidr = "0.0.0.0/0"

# Optional: bring your own keypair instead of letting Terraform generate one.
# ssh_public_key = "ssh-rsa AAAA... you@laptop"

# If your AWS account already has the GitHub Actions OIDC provider, avoid a
# duplicate-provider error:
# create_oidc_provider       = false
# existing_oidc_provider_arn = "arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com"
