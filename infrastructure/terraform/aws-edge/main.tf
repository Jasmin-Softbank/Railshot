terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

locals {
  dns_routes = { for key, route in var.routes : key => route if route.manage_dns }
  route_hosts_valid = alltrue([for r in values(var.routes) :
    (r.host == var.base_domain && var.apex_certificate_arn != null) ||
    (endswith(r.host, ".${var.base_domain}") && length(split(".", r.host)) == length(split(".", var.base_domain)) + 1)
  ])
  gcp_targets                  = toset([for r in values(var.routes) : r.target_private_ip if r.provider_kind == "gcp"])
  wireguard_routing_configured = length(local.gcp_targets) == 0 || (length(var.wireguard_peer_cidrs) > 0 && length(var.wireguard_route_table_ids) > 0)
  aws_target_rules = { for pair in toset([for r in values(var.routes) : "${r.target_security_group_id}:${r.node_port}" if r.provider_kind == "aws"]) :
    pair => { security_group_id = split(":", pair)[0], port = tonumber(split(":", pair)[1]) }
  }
  gcp_ports = toset([for r in values(var.routes) : tostring(r.node_port) if r.provider_kind == "gcp"])
  private_routes = { for pair in setproduct(var.wireguard_route_table_ids, local.gcp_targets) :
    "${pair[0]}:${pair[1]}" => { table = pair[0], ip = pair[1] }
  }
}

data "aws_subnet" "alb" {
  for_each = var.public_subnet_ids
  id       = each.value
}
data "aws_route53_zone" "public" {
  count   = var.zone_id == null ? 0 : 1
  zone_id = var.zone_id
}
resource "aws_route53_zone" "app" {
  count = var.zone_id == null ? 1 : 0
  name  = var.base_domain
  lifecycle { prevent_destroy = true }
}
locals {
  zone_id      = var.zone_id == null ? aws_route53_zone.app[0].zone_id : data.aws_route53_zone.public[0].zone_id
  name_servers = var.zone_id == null ? aws_route53_zone.app[0].name_servers : data.aws_route53_zone.public[0].name_servers
}
data "aws_network_interface" "wireguard" { id = var.wireguard_network_interface_id }
data "aws_instance" "wireguard" {
  instance_id = one(data.aws_network_interface.wireguard.attachment).instance_id
}
data "aws_route_table" "wireguard" {
  for_each       = var.wireguard_route_table_ids
  route_table_id = each.value
}

# This module exclusively owns all inline rules on the ALB SG. External target
# and operations SGs are separate rules-only groups attached by their owners.
resource "aws_security_group" "alb" {
  name_prefix = "${var.name}-alb-"
  vpc_id      = var.vpc_id
  dynamic "ingress" {
    for_each = var.http_redirect ? [80, 443] : [443]
    content {
      protocol    = "tcp"
      from_port   = ingress.value
      to_port     = ingress.value
      cidr_blocks = var.web_client_cidrs
    }
  }
  dynamic "egress" {
    for_each = var.routes
    content {
      description = ""
      protocol    = "tcp"
      from_port   = egress.value.node_port
      to_port     = egress.value.node_port
      cidr_blocks = ["${egress.value.target_private_ip}/32"]
    }
  }
  dynamic "egress" {
    for_each = var.openstack_proxy_egress == null ? [] : [var.openstack_proxy_egress]
    content {
      description = "OpenStack HTTPS proxy on existing control node"
      protocol    = "tcp"
      from_port   = egress.value.node_port
      to_port     = egress.value.node_port
      cidr_blocks = ["${egress.value.target_private_ip}/32"]
    }
  }
  dynamic "egress" {
    for_each = var.openstack_app_egress == null ? [] : [var.openstack_app_egress]
    content {
      description = egress.value.description
      protocol    = "tcp"
      from_port   = egress.value.port
      to_port     = egress.value.port
      cidr_blocks = ["${egress.value.target_private_ip}/32"]
    }
  }
}
resource "aws_security_group_rule" "target_from_alb" {
  for_each                 = local.aws_target_rules
  type                     = "ingress"
  protocol                 = "tcp"
  from_port                = each.value.port
  to_port                  = each.value.port
  security_group_id        = each.value.security_group_id
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_lb" "app" {
  name                       = var.name
  internal                   = false
  load_balancer_type         = "application"
  subnets                    = var.public_subnet_ids
  security_groups            = [aws_security_group.alb.id]
  idle_timeout               = var.idle_timeout
  drop_invalid_header_fields = true
  lifecycle {
    precondition {
      condition     = alltrue([for s in data.aws_subnet.alb : s.vpc_id == var.vpc_id]) && length(toset([for s in data.aws_subnet.alb : s.availability_zone])) >= 2
      error_message = "ALB subnets must belong to the registered VPC and span at least two AZs."
    }
    precondition {
      condition     = var.zone_id == null ? true : (!data.aws_route53_zone.public[0].private_zone && trimsuffix(data.aws_route53_zone.public[0].name, ".") == var.base_domain)
      error_message = "The existing Route53 zone must be public and match base_domain."
    }
    precondition {
      condition     = local.route_hosts_valid
      error_message = "Use a direct child hostname, or base_domain with an explicit apex certificate."
    }
  }
}
resource "aws_lb_target_group" "app" {
  for_each    = var.routes
  name_prefix = "rsapp-"
  vpc_id      = var.vpc_id
  protocol    = "HTTP"
  port        = each.value.node_port
  target_type = "ip"
  health_check {
    path    = each.value.health_path
    matcher = "200-399"
  }
  lifecycle { create_before_destroy = true }
}
resource "aws_lb_target_group_attachment" "app" {
  for_each          = var.routes
  target_group_arn  = aws_lb_target_group.app[each.key].arn
  target_id         = each.value.target_private_ip
  port              = each.value.node_port
  availability_zone = each.value.provider_kind == "gcp" ? "all" : null
  depends_on        = [aws_route.gcp]
}

# Existing certificates are allowed; otherwise one wildcard serves all app routes.
resource "aws_acm_certificate" "app" {
  count             = var.certificate_arn == null ? 1 : 0
  domain_name       = "*.${var.base_domain}"
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}
resource "aws_route53_record" "certificate_validation" {
  count   = var.certificate_arn == null ? 1 : 0
  zone_id = local.zone_id
  name    = one(aws_acm_certificate.app[0].domain_validation_options).resource_record_name
  type    = one(aws_acm_certificate.app[0].domain_validation_options).resource_record_type
  records = [one(aws_acm_certificate.app[0].domain_validation_options).resource_record_value]
  ttl     = 60
}
resource "aws_acm_certificate_validation" "app" {
  count                   = var.certificate_arn == null ? 1 : 0
  certificate_arn         = aws_acm_certificate.app[0].arn
  validation_record_fqdns = [aws_route53_record.certificate_validation[0].fqdn]
}
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn == null ? aws_acm_certificate_validation.app[0].certificate_arn : var.certificate_arn
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      status_code  = "404"
    }
  }
}
resource "aws_lb_listener" "http_redirect" {
  count             = var.http_redirect ? 1 : 0
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
resource "aws_lb_listener_certificate" "apex" {
  count           = var.apex_certificate_arn == null ? 0 : 1
  listener_arn    = aws_lb_listener.https.arn
  certificate_arn = var.apex_certificate_arn
  lifecycle {
    precondition {
      condition     = startswith(var.apex_certificate_arn, "arn:aws:acm:${var.region}:${var.account_id}:certificate/")
      error_message = "The apex certificate must belong to the registered account and ALB region."
    }
  }
}
resource "aws_lb_listener_rule" "app" {
  for_each     = var.routes
  listener_arn = aws_lb_listener.https.arn
  priority     = each.value.priority
  condition {
    host_header { values = [each.value.host] }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app[each.key].arn
  }
}
resource "aws_route53_record" "app" {
  for_each = local.dns_routes
  zone_id  = local.zone_id
  name     = each.value.host
  type     = "A"
  alias {
    name                   = aws_lb.app.dns_name
    zone_id                = aws_lb.app.zone_id
    evaluate_target_health = true
  }
}

# The platform operations node is the WireGuard gateway. Its EC2/ENI stays
# owned by its provisioning module; this module only associates the edge EIP.
resource "aws_eip" "wireguard" {
  domain = "vpc"
  tags   = { Name = "${var.name}-wireguard" }
}
resource "aws_eip_association" "wireguard" {
  allocation_id        = aws_eip.wireguard.id
  network_interface_id = var.wireguard_network_interface_id
  allow_reassociation  = false
  lifecycle {
    precondition {
      condition = (data.aws_network_interface.wireguard.vpc_id == var.vpc_id &&
        data.aws_network_interface.wireguard.owner_id == var.account_id &&
        contains(data.aws_network_interface.wireguard.security_groups, var.wireguard_security_group_id) &&
      data.aws_instance.wireguard.network_interface_id == var.wireguard_network_interface_id)
      error_message = "WireGuard must use the registered operations node primary ENI with its dedicated rules-only SG attached."
    }
    precondition {
      condition     = local.wireguard_routing_configured && (length(local.gcp_targets) == 0 || !data.aws_instance.wireguard.source_dest_check)
      error_message = "GCP forwarding requires known peer /32 endpoints, every ALB subnet route table and source_dest_check=false on the operations instance."
    }
  }
}
resource "aws_security_group_rule" "wireguard_in" {
  count             = length(var.wireguard_peer_cidrs) == 0 ? 0 : 1
  type              = "ingress"
  protocol          = "udp"
  from_port         = 51820
  to_port           = 51820
  security_group_id = var.wireguard_security_group_id
  cidr_blocks       = var.wireguard_peer_cidrs
}
resource "aws_security_group_rule" "wireguard_out" {
  count             = length(var.wireguard_peer_cidrs) == 0 ? 0 : 1
  type              = "egress"
  protocol          = "udp"
  from_port         = 51820
  to_port           = 51820
  security_group_id = var.wireguard_security_group_id
  cidr_blocks       = var.wireguard_peer_cidrs
}
# Routed traffic reaches the middlebox with its original ALB subnet source IP.
resource "aws_security_group_rule" "forward_from_alb" {
  for_each          = local.gcp_ports
  type              = "ingress"
  protocol          = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  security_group_id = var.wireguard_security_group_id
  cidr_blocks       = [for s in data.aws_subnet.alb : s.cidr_block]
}
resource "aws_route" "gcp" {
  for_each               = local.private_routes
  route_table_id         = each.value.table
  destination_cidr_block = "${each.value.ip}/32"
  network_interface_id   = var.wireguard_network_interface_id
  depends_on             = [aws_eip_association.wireguard]
  lifecycle {
    precondition {
      condition     = data.aws_route_table.wireguard[each.value.table].vpc_id == var.vpc_id
      error_message = "WireGuard routes must be in registered VPC route tables."
    }
  }
}

output "app_urls" { value = { for key, r in var.routes : key => "https://${r.host}" } }
output "zone_id" { value = local.zone_id }
output "name_servers" { value = sort(local.name_servers) }
output "target_group_arns" { value = { for key, group in aws_lb_target_group.app : key => group.arn } }
output "alb_dns_name" { value = aws_lb.app.dns_name }
output "alb_security_group_id" { value = aws_security_group.alb.id }
output "wireguard_endpoint" { value = "${aws_eip.wireguard.public_ip}:51820" }
output "wireguard_public_ip" { value = aws_eip.wireguard.public_ip }
output "gcp_private_routes" { value = local.private_routes }
output "certificate_arn" { value = aws_lb_listener.https.certificate_arn }
output "readiness" { value = "configured-references-only; runtime, tunnel and public HTTP unverified" }
