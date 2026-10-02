terraform {
  required_version = ">= 1.7.0, < 2.0"
  required_providers {
    google = { source = "hashicorp/google", version = "= 8.5.0" }
  }
}

provider "google" {
  project = var.project_id
  zone    = var.zone
}

data "google_compute_instance" "backend" {
  name = var.instance_name
  zone = var.zone
}

locals {
  nic         = data.google_compute_instance.backend.network_interface[0]
  gfe_sources = ["35.191.0.0/16", "130.211.0.0/22"]
}

resource "google_project_service" "certificates" {
  service            = "certificatemanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_firewall" "gfe" {
  name                    = "${var.name}-gfe"
  network                 = local.nic.network
  direction               = "INGRESS"
  source_ranges           = local.gfe_sources
  target_service_accounts = [one(data.google_compute_instance.backend.service_account).email]
  allow {
    protocol = "tcp"
    ports    = [tostring(var.node_port)]
  }
}

resource "google_compute_network_endpoint_group" "app" {
  name                  = var.name
  zone                  = var.zone
  network               = local.nic.network
  subnetwork            = local.nic.subnetwork
  network_endpoint_type = "GCE_VM_IP_PORT"
  default_port          = var.node_port
  lifecycle {
    precondition {
      condition     = length(data.google_compute_instance.backend.network_interface) == 1 && local.nic.network_ip == var.expected_private_ip
      error_message = "The existing VM must have one NIC with the reviewed private IP."
    }
  }
}

resource "google_compute_network_endpoint" "app" {
  network_endpoint_group = google_compute_network_endpoint_group.app.name
  zone                   = var.zone
  instance               = data.google_compute_instance.backend.name
  ip_address             = local.nic.network_ip
  port                   = var.node_port
}

resource "google_compute_health_check" "app" {
  name = var.name
  http_health_check {
    port         = var.node_port
    request_path = var.health_path
    host         = var.hostname
  }
}

# ponytail: one existing VM remains a failure domain; add healthy endpoints for HA.
resource "google_compute_backend_service" "app" {
  name                  = var.name
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  health_checks         = [google_compute_health_check.app.id]
  backend {
    group                 = google_compute_network_endpoint_group.app.id
    balancing_mode        = "RATE"
    max_rate_per_endpoint = 100
  }
}

resource "google_compute_url_map" "app" {
  name = var.name
  default_url_redirect {
    host_redirect = var.hostname
    strip_query   = false
  }
  host_rule {
    hosts        = [var.hostname]
    path_matcher = "app"
  }
  path_matcher {
    name            = "app"
    default_service = google_compute_backend_service.app.id
  }
}

resource "google_certificate_manager_dns_authorization" "app" {
  name       = var.name
  domain     = var.hostname
  type       = "PER_PROJECT_RECORD"
  depends_on = [google_project_service.certificates]
}

resource "google_certificate_manager_certificate" "app" {
  name = var.name
  managed {
    domains            = [var.hostname]
    dns_authorizations = [google_certificate_manager_dns_authorization.app.id]
  }
}

resource "google_certificate_manager_certificate_map" "app" {
  name       = var.name
  depends_on = [google_project_service.certificates]
}

resource "google_certificate_manager_certificate_map_entry" "app" {
  name         = var.name
  map          = google_certificate_manager_certificate_map.app.name
  hostname     = var.hostname
  certificates = [google_certificate_manager_certificate.app.id]
}

resource "google_compute_target_https_proxy" "app" {
  name            = var.name
  url_map         = google_compute_url_map.app.id
  certificate_map = "//certificatemanager.googleapis.com/${google_certificate_manager_certificate_map.app.id}"
}

resource "google_compute_global_address" "app" {
  name         = var.name
  address_type = "EXTERNAL"
  ip_version   = "IPV4"
}

resource "google_compute_global_forwarding_rule" "https" {
  name                  = "${var.name}-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_https_proxy.app.id
  ip_address            = google_compute_global_address.app.id
  port_range            = "443"
}

resource "google_compute_url_map" "redirect" {
  name = "${var.name}-redirect"
  default_url_redirect {
    host_redirect          = var.hostname
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  name    = "${var.name}-redirect"
  url_map = google_compute_url_map.redirect.id
}

resource "google_compute_global_forwarding_rule" "http" {
  name                  = "${var.name}-http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_http_proxy.redirect.id
  ip_address            = google_compute_global_address.app.id
  port_range            = "80"
}

output "frontend_ip" { value = google_compute_global_address.app.address }
output "dns_authorization_record" { value = google_certificate_manager_dns_authorization.app.dns_resource_record[0] }
output "health_url" { value = "https://${var.hostname}${var.health_path}" }
output "backend_service" { value = google_compute_backend_service.app.name }
output "required_workload_ingress_cidrs" { value = local.gfe_sources }
output "readiness" { value = "configured-references-only; certificate, backend health and public HTTPS require live verification" }
