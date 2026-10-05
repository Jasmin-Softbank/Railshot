# Keep the existing control API rule with its current owner. This separately
# owned rule grants only the new platform agent access to the registered node.
variable "platform_worker_api_source_cidr" {
  type    = string
  default = null
  validation {
    condition     = var.platform_worker_api_source_cidr == null ? true : can(cidrnetmask(var.platform_worker_api_source_cidr)) && endswith(var.platform_worker_api_source_cidr, "/32")
    error_message = "Use the platform worker's exact public IPv4 /32."
  }
}

resource "google_compute_firewall" "platform_worker_api" {
  count                   = var.platform_worker_api_source_cidr == null ? 0 : 1
  project                 = local.gcp_project_id
  name                    = "railshot-gcp-poc-platform-worker-api"
  network                 = "projects/${local.gcp_project_id}/global/networks/railshot-gcp-poc-vpc"
  direction               = "INGRESS"
  source_ranges           = [var.platform_worker_api_source_cidr]
  target_service_accounts = ["railshot-gcp-poc-node@${local.gcp_project_id}.iam.gserviceaccount.com"]
  allow {
    protocol = "tcp"
    ports    = ["6443"]
  }
}
