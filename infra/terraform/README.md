# Terraform — ECR repositories

Provisions one ECR repository per service, with image scanning on push and
a lifecycle policy that expires old images.

**Not included here (deliberately out of scope for this repo):** the VPC,
EKS cluster, and node groups. In most real orgs those are owned by a
separate platform/infrastructure repo and shared across many applications,
not reprovisioned per project. Point `EKS_CLUSTER_NAME` in the GitHub
Actions CD workflow at whatever cluster you have.

```bash
cd infra/terraform
terraform init
terraform plan
terraform apply
```
