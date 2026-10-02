variable "name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,28}[a-z0-9]$", var.name)) && !startswith(var.name, "internal-")
    error_message = "Use a 3-30 character ALB name without internal- prefix."
  }
}
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Use the registered 12-digit AWS account."
  }
}
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" {
  type = set(string)
  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "Provide public subnets in at least two distinct availability zones."
  }
}
variable "routes" {
  description = "Administrator-assigned AWS app routes; one IP target per app. Reuse customer nodes with distinct app NodePorts."
  type = map(object({
    host                     = string
    provider_kind            = string
    target_private_ip        = string
    node_port                = number
    health_path              = string
    priority                 = number
    target_security_group_id = optional(string)
    manage_dns               = optional(bool, true)
  }))
  validation {
    condition = length(var.routes) > 0 && length(var.routes) <= 50 && alltrue([for key, route in var.routes :
      can(regex("^[a-z][a-z0-9-]{1,39}$", key)) && route.provider_kind == "aws" &&
      can(cidrnetmask("${route.target_private_ip}/32")) &&
      can(regex("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)", route.target_private_ip)) &&
      route.node_port >= 30000 && route.node_port <= 32767 && floor(route.node_port) == route.node_port &&
      route.priority >= 1 && route.priority <= 50000 && floor(route.priority) == route.priority &&
      length(route.health_path) <= 1024 && can(regex("^/[^\\r\\n]*$", route.health_path)) &&
      length(route.host) <= 253 && can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", route.host)) &&
      can(regex("^sg-[0-9a-f]{8,17}$", route.target_security_group_id))
    ])
    error_message = "Use 1-50 named AWS routes with an RFC1918 IPv4, NodePort, health path, DNS host, priority and dedicated target SG. GCP uses its native L7 entrypoint; WireGuard routes are retired."
  }
  validation {
    condition     = length(distinct([for r in values(var.routes) : r.host])) == length(var.routes) && length(distinct([for r in values(var.routes) : r.priority])) == length(var.routes)
    error_message = "Route hostnames and listener priorities must be unique."
  }
}
variable "zone_id" {
  type        = string
  default     = null
  description = "Existing public Route53 zone, or null to create one for base_domain. Domain registration and NS delegation remain separate. Keep null after creating a managed zone."
  validation {
    condition     = var.zone_id == null ? true : can(regex("^Z[A-Z0-9]+$", var.zone_id))
    error_message = "Use an existing Route53 zone ID or null to create the public zone."
  }
}
variable "base_domain" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", var.base_domain))
    error_message = "Use the registered lowercase base domain, without a wildcard or trailing dot."
  }
}
variable "certificate_arn" {
  type        = string
  default     = null
  description = "Existing regional ACM certificate covering all route hosts, or null to create a DNS-validated wildcard for base_domain."
  validation {
    condition     = var.certificate_arn == null ? true : can(regex("^arn:aws:acm:[a-z0-9-]+:[0-9]{12}:certificate/", var.certificate_arn))
    error_message = "Use a regional ACM certificate ARN or null."
  }
}
variable "idle_timeout" {
  type        = number
  default     = 60
  description = "Shared ALB connection idle timeout in seconds; set above the longest supported API response wait."
  validation {
    condition     = var.idle_timeout >= 1 && var.idle_timeout <= 4000 && floor(var.idle_timeout) == var.idle_timeout
    error_message = "Use an integer ALB idle timeout from 1 through 4000 seconds."
  }
}
variable "http_redirect" {
  type    = bool
  default = false
}
variable "apex_certificate_arn" {
  type        = string
  default     = null
  description = "Additional ISSUED regional ACM certificate for base_domain itself. Keeps the existing wildcard/default certificate and customer routes intact."
  validation {
    condition     = var.apex_certificate_arn == null ? true : can(regex("^arn:aws:acm:[a-z0-9-]+:[0-9]{12}:certificate/[a-f0-9-]+$", var.apex_certificate_arn))
    error_message = "Use the verified regional ACM certificate ARN for the apex hostname."
  }
}
variable "web_client_cidrs" {
  type    = set(string)
  default = ["0.0.0.0/0"]
  validation {
    condition     = length(var.web_client_cidrs) > 0 && alltrue([for c in var.web_client_cidrs : can(cidrnetmask(c))])
    error_message = "Provide valid IPv4 client CIDRs."
  }
}

# Rejection sentinel for old tfvars, not a transport option.
variable "wireguard_network_interface_id" {
  type        = string
  default     = null
  description = "Retired input: remove this field after the reviewed WireGuard migration."
  validation {
    condition     = var.wireguard_network_interface_id == null
    error_message = "WireGuard is retired; complete the documented migration and remove wireguard_network_interface_id."
  }
}

# Rejection sentinel for old tfvars, not a transport option.
variable "wireguard_security_group_id" {
  type        = string
  default     = null
  description = "Retired input: remove this field after the reviewed WireGuard migration."
  validation {
    condition     = var.wireguard_security_group_id == null
    error_message = "WireGuard is retired; complete the documented migration and remove wireguard_security_group_id."
  }
}

# Rejection sentinel for old tfvars, not a transport option.
variable "wireguard_peer_cidrs" {
  type        = set(string)
  default     = []
  description = "Retired input: remove this field after the reviewed WireGuard migration."
  validation {
    condition     = length(var.wireguard_peer_cidrs) == 0
    error_message = "WireGuard is retired; complete the documented migration and remove wireguard_peer_cidrs."
  }
}

# Rejection sentinel for old tfvars, not a transport option.
variable "wireguard_route_table_ids" {
  type        = set(string)
  default     = []
  description = "Retired input: remove this field after the reviewed WireGuard migration."
  validation {
    condition     = length(var.wireguard_route_table_ids) == 0
    error_message = "WireGuard is retired; complete the documented migration and remove wireguard_route_table_ids."
  }
}
