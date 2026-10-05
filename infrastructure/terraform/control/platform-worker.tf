# One ordinary agent separates Argo/UI work from the API's existing local PVC.
# Join is performed over SSM after networking is ready; tokens never enter state.
variable "platform_worker_enabled" {
  type    = bool
  default = false
}
variable "platform_worker_stop_at" {
  type        = string
  default     = null
  description = "Absolute UTC stop deadline for the temporary platform agent, matching the approved operations window."
  validation {
    condition     = var.platform_worker_stop_at == null ? true : can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.platform_worker_stop_at)) && can(timecmp(var.platform_worker_stop_at, var.platform_worker_stop_at))
    error_message = "Use an exact UTC stop deadline, or null when the worker is disabled."
  }
}
variable "platform_worker_external_api_cidrs" {
  type        = set(string)
  default     = []
  description = "Registered external customer API IPv4 /32 destinations for the platform agent."
  validation {
    condition     = length(var.platform_worker_external_api_cidrs) <= 20 && alltrue([for cidr in var.platform_worker_external_api_cidrs : can(cidrnetmask(cidr)) && endswith(cidr, "/32")])
    error_message = "Use exact registered external API IPv4 /32 destinations."
  }
}

resource "aws_security_group" "operations_peer" {
  count       = var.platform_worker_enabled ? 1 : 0
  name        = "railshot-operations-peer"
  description = "Private K3s and Cilium traffic between operations nodes"
  vpc_id      = var.vpc_id
  dynamic "ingress" {
    for_each = [{ protocol = "tcp", port = 6443 }, { protocol = "udp", port = 8472 }, { protocol = "tcp", port = 4240 }]
    content {
      protocol  = ingress.value.protocol
      from_port = ingress.value.port
      to_port   = ingress.value.port
      self      = true
    }
  }
  dynamic "ingress" {
    for_each = toset([31490, 31491])
    content {
      protocol        = "tcp"
      from_port       = ingress.value
      to_port         = ingress.value
      security_groups = [aws_security_group.control.id]
      description     = "Existing control observer node and cluster metrics"
    }
  }
  ingress {
    protocol  = "icmp"
    from_port = 8
    to_port   = 0
    self      = true
  }
  egress {
    protocol  = "-1"
    from_port = 0
    to_port   = 0
    self      = true
  }
}

resource "aws_security_group" "platform_worker" {
  count       = var.platform_worker_enabled ? 1 : 0
  name        = "railshot-platform-worker"
  description = "Platform agent outbound registry, SSM and registered customer APIs"
  vpc_id      = var.vpc_id
  dynamic "egress" {
    for_each = [80, 443]
    content {
      protocol    = "tcp"
      from_port   = egress.value
      to_port     = egress.value
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
  egress {
    protocol        = "tcp"
    from_port       = 6443
    to_port         = 6443
    security_groups = [aws_security_group.user_aws.id]
  }
  dynamic "egress" {
    for_each = var.platform_worker_external_api_cidrs
    content {
      protocol    = "tcp"
      from_port   = 6443
      to_port     = 6443
      cidr_blocks = [egress.value]
    }
  }
}

resource "aws_security_group_rule" "platform_user_api" {
  count                    = var.platform_worker_enabled ? 1 : 0
  type                     = "ingress"
  protocol                 = "tcp"
  from_port                = 6443
  to_port                  = 6443
  security_group_id        = aws_security_group.user_aws.id
  source_security_group_id = aws_security_group.platform_worker[0].id
}

resource "aws_iam_role" "platform_worker" {
  count = var.platform_worker_enabled ? 1 : 0
  name  = "railshot-platform-worker"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "platform_worker_ssm" {
  count      = var.platform_worker_enabled ? 1 : 0
  role       = aws_iam_role.platform_worker[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "platform_worker_no_credentials" {
  count = var.platform_worker_enabled ? 1 : 0
  role  = aws_iam_role.platform_worker[0].id
  name  = "deny-application-credentials"
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Deny", Action = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "secretsmanager:GetSecretValue"], Resource = "*"
  }] })
}
resource "aws_iam_instance_profile" "platform_worker" {
  count = var.platform_worker_enabled ? 1 : 0
  name  = "railshot-platform-worker"
  role  = aws_iam_role.platform_worker[0].name
}

resource "aws_instance" "platform_worker" {
  count                                = var.platform_worker_enabled ? 1 : 0
  ami                                  = data.aws_ami.ubuntu.id
  instance_type                        = "m6i.large"
  subnet_id                            = var.subnet_id
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.operations_peer[0].id, aws_security_group.platform_worker[0].id]
  iam_instance_profile                 = aws_iam_instance_profile.platform_worker[0].name
  instance_initiated_shutdown_behavior = "stop"
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
  user_data = <<-CLOUD
    #cloud-config
    write_files:
      - path: /etc/systemd/system/railshot-platform-stop.service
        permissions: '0644'
        content: |
          [Unit]
          Description=Stop temporary Railshot platform agent, retain EBS
          [Service]
          Type=oneshot
          ExecStart=/usr/bin/systemctl poweroff
      - path: /etc/systemd/system/railshot-platform-stop.timer
        permissions: '0644'
        content: |
          [Unit]
          Description=Approved Railshot operations deadline
          [Timer]
          OnCalendar=${var.platform_worker_stop_at == null ? "" : replace(replace(var.platform_worker_stop_at, "T", " "), "Z", " UTC")}
          Persistent=true
          AccuracySec=1s
          Unit=railshot-platform-stop.service
          [Install]
          WantedBy=timers.target
    runcmd:
      - [systemctl, daemon-reload]
      - [systemctl, enable, --now, railshot-platform-stop.timer]
  CLOUD
  tags      = { Name = "railshot-platform-worker-aws-01", Component = "platform-worker" }
  depends_on = [
    aws_iam_role_policy_attachment.platform_worker_ssm,
    aws_iam_role_policy.platform_worker_no_credentials,
  ]
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = var.platform_worker_stop_at == null ? false : timecmp(var.platform_worker_stop_at, timestamp()) > 0
      error_message = "An enabled platform worker requires a future approved stop deadline."
    }
  }
}

output "operations_peer_security_group_id" { value = one(aws_security_group.operations_peer[*].id) }
output "platform_worker" {
  value = var.platform_worker_enabled ? {
    instance_id = aws_instance.platform_worker[0].id
    private_ip  = aws_instance.platform_worker[0].private_ip
    public_ip   = aws_instance.platform_worker[0].public_ip
    node_name   = "railshot-platform-worker-aws-01"
    stop_at     = var.platform_worker_stop_at
  } : null
}
