# Minimal, deliberately cheap networking: ONE public subnet, an internet
# gateway, and a route table. There is no NAT gateway and no private subnet —
# that would cost ~$32/month on its own, and this single-node setup does not
# need it. The node gets a public IP and egresses directly through the IGW.
#
# Everything below (VPC, subnet, IGW, route table, security group) is free;
# the only recurring cost is the EC2 instance and its EBS volume.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  common_tags = merge(
    {
      Project   = var.project_name
      ManagedBy = "terraform"
    },
    var.tags,
  )
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, { Name = "${var.project_name}-vpc" })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.common_tags, { Name = "${var.project_name}-igw" })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, { Name = "${var.project_name}-public" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-public-rt" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# Security group
#
# 22  -> SSH, used by the CD workflow to run `kubectl` on the node
# 80  -> api-gateway, exposed by k3s ServiceLB on the node's public IP.
#        80/443 are also needed so the k3s ServiceLB can serve the gateway
#        without an AWS load balancer (which would add ~$18/month).
# Egress is unrestricted so the node can reach ECR and Docker Hub.
# ---------------------------------------------------------------------------
resource "aws_security_group" "node" {
  name_prefix = "${var.project_name}-node-"
  description = "k3s node: SSH + api-gateway"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  ingress {
    description = "api-gateway via k3s ServiceLB"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.allowed_http_cidr]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.allowed_http_cidr]
  }

  egress {
    description = "All outbound (ECR pulls, Docker Hub, OS updates)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-node-sg" })

  lifecycle {
    create_before_destroy = true
  }
}
