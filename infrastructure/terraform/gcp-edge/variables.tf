variable "project_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use the reviewed GCP project ID."
  }
}
variable "zone" { type = string }
variable "instance_name" { type = string }
variable "expected_private_ip" {
  type = string
  validation {
    condition     = can(cidrnetmask("${var.expected_private_ip}/32"))
    error_message = "Use the existing VM's reviewed IPv4 address."
  }
}
variable "name" {
  type    = string
  default = "railshot-gcp-edge"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,42}[a-z0-9]$", var.name))
    error_message = "Use a lowercase GCP resource name of 2-44 characters."
  }
}
variable "hostname" {
  type = string
  validation {
    condition     = length(var.hostname) <= 253 && can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", var.hostname))
    error_message = "Use one exact lowercase DNS hostname without a wildcard or trailing dot."
  }
}
variable "node_port" {
  type    = number
  default = 30080
  validation {
    condition     = var.node_port == floor(var.node_port) && var.node_port >= 30000 && var.node_port <= 32767
    error_message = "Use the allocated Kubernetes NodePort."
  }
}
variable "health_path" {
  type    = string
  default = "/health"
  validation {
    condition     = can(regex("^/[A-Za-z0-9/._~-]*$", var.health_path)) && !strcontains(var.health_path, "//") && !contains(split("/", var.health_path), "..") && !contains(split("/", var.health_path), ".")
    error_message = "Use a plain absolute HTTP path without query, fragment or traversal."
  }
}
