terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

# Optional AWS edge adapter. Runtime provisioning remains with its owner.
resource "aws_security_group" "alb" {
  name_prefix = "${var.name}-alb-"
  vpc_id      = var.vpc_id
  ingress {
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = var.web_client_cidrs
  }
  egress {
    protocol        = "tcp"
    from_port       = var.target_port
    to_port         = var.target_port
    security_groups = [var.target_security_group_id]
  }
}

resource "aws_security_group_rule" "target_from_alb" {
  type                     = "ingress"
  protocol                 = "tcp"
  from_port                = var.target_port
  to_port                  = var.target_port
  security_group_id        = var.target_security_group_id
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_lb" "app" {
  name                       = var.name
  internal                   = false
  load_balancer_type         = "application"
  subnets                    = var.public_subnet_ids
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "app" {
  name_prefix = "rsapp-"
  vpc_id      = var.vpc_id
  protocol    = "HTTP"
  port        = var.target_port
  target_type = "instance"
  health_check {
    path    = var.health_path
    matcher = "200-399"
  }
}

resource "aws_lb_target_group_attachment" "app" {
  for_each         = var.target_instance_ids
  target_group_arn = aws_lb_target_group.app.arn
  target_id        = each.value
  port             = var.target_port
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      status_code  = "404"
    }
  }
}

resource "aws_lb_listener_rule" "app" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 100
  condition {
    host_header { values = [var.app_domain] }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_route53_record" "app" {
  zone_id = var.zone_id
  name    = var.app_domain
  type    = "A"
  alias {
    name                   = aws_lb.app.dns_name
    zone_id                = aws_lb.app.zone_id
    evaluate_target_health = true
  }
}

# This EIP belongs to the VPN gateway ENI, never to the ALB or NAT gateway.
resource "aws_eip" "wireguard" {
  domain = "vpc"
  tags   = { Name = "${var.name}-wireguard" }
}
resource "aws_eip_association" "wireguard" {
  allocation_id        = aws_eip.wireguard.id
  network_interface_id = var.wireguard_network_interface_id
  allow_reassociation  = false
}
resource "aws_security_group_rule" "wireguard" {
  type              = "ingress"
  protocol          = "udp"
  from_port         = 51820
  to_port           = 51820
  security_group_id = var.wireguard_security_group_id
  cidr_blocks       = var.wireguard_peer_cidrs
}

output "app_url" { value = "https://${var.app_domain}" }
output "alb_dns_name" { value = aws_lb.app.dns_name }
output "wireguard_endpoint" { value = "${aws_eip.wireguard.public_ip}:51820" }
output "readiness" { value = "configured-references-only; runtime, tunnel and public HTTP unverified" }
