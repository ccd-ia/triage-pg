# VPC endpoints so Fargate tasks in private subnets with assignPublicIp=DISABLED (batch.tf) can
# pull the image and ship logs WITHOUT a NAT gateway. Pulling from ECR needs three legs:
#   * ecr.api  — GetAuthorizationToken / registry auth (the leg that timed out pre-fix)
#   * ecr.dkr  — the Docker registry (image manifests)
#   * s3       — the layer BLOBS live in S3 (a GATEWAY endpoint, not interface)
# plus CloudWatch Logs (logs) for the awslogs driver. RDS reachability is a separate concern —
# the Batch job reaches the private RDS in-VPC directly (rds.tf SG), never through egress.
#
# Gated: a prod BYO network that already has egress (a working NAT) leaves create_vpc_endpoints
# false; the throwaway/no-NAT validation footprint flips it true in terraform.tfvars.

data "aws_vpc" "this" {
  id = var.vpc_id
}

locals {
  # Interface endpoints for the two ECR control-plane legs + CloudWatch Logs. S3 (layer blobs)
  # is a GATEWAY endpoint handled separately below.
  interface_endpoint_services = var.create_vpc_endpoints ? toset([
    "ecr.api",
    "ecr.dkr",
    "logs",
  ]) : toset([])
}

resource "aws_security_group" "vpc_endpoints" {
  count       = var.create_vpc_endpoints ? 1 : 0
  name        = "${var.name_prefix}-vpc-endpoints"
  description = "HTTPS from in-VPC compute (Fargate task ENIs) to the interface endpoints"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from within the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoint_services

  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.vpc_endpoints[0].id]
  private_dns_enabled = true # so the default ECR/logs hostnames resolve to the endpoint ENIs
}

# Optional S3 GATEWAY endpoint for VPCs that don't already have one. The validation footprint's
# BYO VPC already has an S3 gateway on its main route table, so endpoint_s3_route_table_ids stays
# empty here (creating a second S3 gateway on the same route table would conflict).
resource "aws_vpc_endpoint" "s3" {
  count             = var.create_vpc_endpoints && length(var.endpoint_s3_route_table_ids) > 0 ? 1 : 0
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.endpoint_s3_route_table_ids
}
