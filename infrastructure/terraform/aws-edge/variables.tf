variable "name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,28}[a-z0-9]$", var.name)) && !startswith(var.name, "internal-")
    error_message = "Use a 3–30 character AWS load balancer name without internal- prefix."
  }
}
variable "vpc_id" { type = string }
variable "public_subnet_ids" {
  type = set(string)
  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "Provide public subnets in at least two distinct availability zones."
  }
}
variable "target_instance_ids" {
  type = set(string)
  validation {
    condition     = length(var.target_instance_ids) > 0
    error_message = "At least one registered AWS instance target is required."
  }
}
variable "target_security_group_id" { type = string }
variable "target_port" {
  type = number
  validation {
    condition     = var.target_port >= 30000 && var.target_port <= 32767 && floor(var.target_port) == var.target_port
    error_message = "Use the explicitly allocated Kubernetes NodePort (30000–32767)."
  }
}
variable "health_path" {
  type = string
  validation {
    condition     = can(regex("^/[^\\r\\n]*$", var.health_path))
    error_message = "Provide a health path beginning with /."
  }
}
variable "certificate_arn" { type = string }
variable "zone_id" { type = string }
variable "app_domain" { type = string }
variable "web_client_cidrs" {
  type    = set(string)
  default = ["0.0.0.0/0"]
  validation {
    condition     = length(var.web_client_cidrs) > 0 && alltrue([for c in var.web_client_cidrs : can(cidrnetmask(c))])
    error_message = "Provide valid IPv4 client CIDRs."
  }
}
variable "wireguard_network_interface_id" { type = string }
variable "wireguard_security_group_id" { type = string }
variable "wireguard_peer_cidrs" {
  type = set(string)
  validation {
    condition     = length(var.wireguard_peer_cidrs) > 0 && alltrue([for c in var.wireguard_peer_cidrs : can(cidrnetmask(c)) && c != "0.0.0.0/0"])
    error_message = "Specify known peer/NAT egress IPv4 CIDRs; no default world-open VPN rule."
  }
}
