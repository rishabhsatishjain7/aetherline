# ---------------------------------------------------------------------------
# Single k3s node. Cheaper than EKS by roughly $73/month (EKS control plane)
# while still being real Kubernetes, so k8s/base stays the source of truth.
# ---------------------------------------------------------------------------

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "tls_private_key" "generated" {
  count     = var.ssh_public_key == "" ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 4096
}

locals {
  ssh_public_key = var.ssh_public_key != "" ? var.ssh_public_key : tls_private_key.generated[0].public_key_openssh
}

resource "aws_key_pair" "node" {
  key_name_prefix = "${var.project_name}-"
  public_key      = local.ssh_public_key

  tags = local.common_tags
}

# Written locally (never into the repo — *.pem is gitignored) only when
# Terraform generated the keypair itself.
resource "local_sensitive_file" "private_key" {
  count           = var.ssh_public_key == "" ? 1 : 0
  content         = tls_private_key.generated[0].private_key_pem
  filename        = var.ssh_private_key_path
  file_permission = "0600"
}

resource "aws_instance" "node" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.node.id]
  key_name               = aws_key_pair.node.key_name

  user_data                   = templatefile("${path.module}/user_data.sh.tpl", { k3s_channel = var.k3s_channel })
  user_data_replace_on_change = true

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required" # IMDSv2 only
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-k3s" })
}

# Stable address so DEPLOY_HOST in GitHub Actions never changes when the
# instance is stopped/started. Free while attached to a running instance.
resource "aws_eip" "node" {
  domain   = "vpc"
  instance = aws_instance.node.id

  tags = merge(local.common_tags, { Name = "${var.project_name}-eip" })

  depends_on = [aws_internet_gateway.main]
}
