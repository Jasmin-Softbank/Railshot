# Restored existing operations node; customer app nodes and isolated CI builder are separate.
terraform {
  required_version = ">= 1.5.7"
  backend "local" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.66.0" }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags { tags = { Project = "railshot", Component = "control-poc", ManagedBy = "terraform" } }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Use the existing registered AWS account."
  }
}
variable "vpc_id" { type = string }
variable "subnet_id" {
  type        = string
  description = "Exact existing operations-node subnet; do not select a new default subnet."
}
data "aws_subnet" "control" { id = var.subnet_id }
variable "ami_id" {
  type        = string
  description = "Reviewed exact Canonical Ubuntu 24.04 amd64 image; required for future plans. Existing VM is not changed by this source edit."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Provide an exact regional AMI ID, never a moving image alias."
  }
}
data "aws_ami" "ubuntu" {
  owners = ["099720109477"]
  filter {
    name   = "image-id"
    values = [var.ami_id]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}
data "aws_caller_identity" "current" {}

locals {
  name           = "railshot-control-poc"
  auth_parameter = "/railshot/codex/operator-primary/auth"
  pull_parameter = "/railshot/registry/ghcr/pull_token"
  readable_parameters = [for path in [local.auth_parameter, local.pull_parameter] :
  "arn:aws:ssm:${var.region}:${var.account_id}:parameter${path}"]
}

resource "aws_security_group" "control" {
  name        = local.name
  description = "Administrator PoC: no ingress; SSM and HTTPS outbound"
  vpc_id      = var.vpc_id
  ingress     = []
  dynamic "egress" {
    for_each = [80, 443]
    content {
      from_port   = egress.value
      to_port     = egress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
}

# No inline ingress/egress: aws-edge owns this group's explicit rules.
# The original base SG remains unchanged and is never passed to aws-edge.
resource "aws_security_group" "edge" {
  name        = "${local.name}-edge"
  description = "RAILSHOT edge rules managed by aws-edge"
  vpc_id      = var.vpc_id
}

resource "aws_iam_role" "control" {
  name = local.name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.control.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "codex_auth" {
  role = aws_iam_role.control.id
  name = "read-single-operator-auth"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = local.readable_parameters
      }, {
      # SSMManagedInstanceCore itself grants parameter reads on '*'. Narrow those too.
      Effect      = "Deny"
      Action      = ["ssm:GetParameter", "ssm:GetParameters"]
      NotResource = local.readable_parameters
      }, {
      Effect   = "Deny"
      Action   = ["ssm:GetParametersByPath", "ssm:GetParameterHistory"]
      Resource = "*"
    }]
  })
}
resource "aws_iam_instance_profile" "control" {
  name = local.name
  role = aws_iam_role.control.name
}

resource "aws_instance" "control" {
  ami                                  = data.aws_ami.ubuntu.id
  instance_type                        = "t3.medium"
  subnet_id                            = var.subnet_id
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.control.id, aws_security_group.edge.id]
  source_dest_check                    = false
  iam_instance_profile                 = aws_iam_instance_profile.control.name
  instance_initiated_shutdown_behavior = "stop"
  credit_specification { cpu_credits = "standard" }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = false
  }
  # Preserve the existing bootstrap bytes. This recovery does not rerun or
  # reconstruct legacy cloud-init/Ansible/accounts files in their old layout.
  user_data_replace_on_change = false
  tags                        = { Name = local.name }
  depends_on                  = [aws_iam_role_policy_attachment.ssm, aws_iam_role_policy.codex_auth]
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [user_data, user_data_base64]
    precondition {
      condition     = data.aws_subnet.control.vpc_id == var.vpc_id
      error_message = "The existing operations subnet must belong to the registered VPC."
    }
  }
}

output "instance_id" { value = aws_instance.control.id }
output "auth_parameter_name" { value = local.auth_parameter }
output "connect" { value = "aws ssm start-session --region ${var.region} --target ${aws_instance.control.id}" }

output "private_ip" { value = aws_instance.control.private_ip }
output "primary_network_interface_id" { value = aws_instance.control.primary_network_interface_id }
output "edge_security_group_id" { value = aws_security_group.edge.id }
output "source_dest_check" { value = aws_instance.control.source_dest_check }
