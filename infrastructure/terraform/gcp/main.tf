locals {
  labels    = { project = "railshot", environment = "poc", component = "app-node", managed_by = "terraform" }
  web_ports = concat(var.allow_http ? ["80"] : [], var.allow_https ? ["443"] : [])
  # OAuth scope permits the metadata credential flow; repository IAM is owned
  # separately and must grant only the registered Artifact Registry repository.
  node_oauth_scopes = var.enable_gcp_registry_pull ? ["https://www.googleapis.com/auth/devstorage.read_only"] : []
  iap_ssh = var.allow_iap_ssh ? {
    source_ranges = ["35.235.240.0/20"]
    ports         = ["22"]
    transport_ref = "iap:${var.project_id}/${var.zone}/${var.name}"
  } : null
  runtime_limit = var.max_run_duration_seconds == null ? null : {
    seconds                     = var.max_run_duration_seconds
    instance_termination_action = "STOP"
    automatic_restart           = false
  }
  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    bootstrap_manifest = jsonencode({ method = "host-preparation-only", runtime_status = "not_configured" })
    host_config = yamlencode({
      name           = var.name
      node_name      = coalesce(var.node_name, var.name)
      cloud_provider = "gcp"
      region         = var.region
      runtime_status = "not_configured"
    })
    bootstrap_script = templatefile("${path.module}/bootstrap.sh.tftpl", {
      initialize_empty_data_disk = var.initialize_empty_data_disk ? "true" : "false"
    })
  })
}

resource "google_compute_network" "node" {
  name                            = "${var.name}-vpc"
  auto_create_subnetworks         = false
  routing_mode                    = "REGIONAL"
  delete_default_routes_on_create = false
}

resource "google_compute_subnetwork" "node" {
  name                     = "${var.name}-subnet"
  region                   = var.region
  network                  = google_compute_network.node.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

# No project/resource IAM role bindings, service account keys, or credential metadata.
resource "google_service_account" "node" {
  account_id   = "${var.name}-node"
  display_name = "RAILSHOT app node (no API roles)"
}

resource "google_compute_firewall" "web" {
  count                   = length(local.web_ports) > 0 ? 1 : 0
  name                    = "${var.name}-web"
  network                 = google_compute_network.node.name
  direction               = "INGRESS"
  source_ranges           = var.web_source_ranges
  target_service_accounts = [google_service_account.node.email]
  allow {
    protocol = "tcp"
    ports    = local.web_ports
  }
}

# IAP is an authenticated administrative transport; public TCP 22 is never enabled.
resource "google_compute_firewall" "iap_ssh" {
  count                   = local.iap_ssh == null ? 0 : 1
  name                    = "${var.name}-iap-ssh"
  network                 = google_compute_network.node.name
  direction               = "INGRESS"
  source_ranges           = local.iap_ssh.source_ranges
  target_service_accounts = [google_service_account.node.email]
  allow {
    protocol = "tcp"
    ports    = local.iap_ssh.ports
  }
}

# Stable egress/app IP. Retaining a reserved IP incurs charges even if the VM is stopped.
resource "google_compute_address" "node" {
  name   = "${var.name}-ip"
  region = var.region
}

resource "google_compute_disk" "data" {
  name            = "${var.name}-data"
  zone            = var.zone
  type            = "pd-balanced"
  size            = var.data_disk_gib
  labels          = merge(local.labels, { purpose = "persistent-data" })
  deletion_policy = "PREVENT"
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_instance" "node" {
  name                      = var.name
  zone                      = var.zone
  machine_type              = var.machine_type
  allow_stopping_for_update = false
  labels                    = local.labels

  # No desired_status = "RUNNING": a cost-cutoff stop must not be undone by apply.
  dynamic "scheduling" {
    for_each = local.runtime_limit == null ? [] : [local.runtime_limit]
    content {
      provisioning_model          = "STANDARD"
      automatic_restart           = scheduling.value.automatic_restart
      instance_termination_action = scheduling.value.instance_termination_action
      max_run_duration {
        seconds = scheduling.value.seconds
      }
    }
  }

  boot_disk {
    auto_delete = true
    initialize_params {
      image  = var.boot_image
      type   = "pd-balanced"
      size   = var.boot_disk_gib
      labels = local.labels
    }
  }
  # Existing attached disks are not auto-deleted by the compute_instance provider.
  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "railshot-data"
    mode        = "READ_WRITE"
  }
  network_interface {
    subnetwork = google_compute_subnetwork.node.id
    access_config {
      nat_ip = google_compute_address.node.address
    }
  }
  service_account {
    email  = google_service_account.node.email
    scopes = local.node_oauth_scopes
  }
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
  metadata = {
    "block-project-ssh-keys" = "true"
    "enable-oslogin"         = "true"
    "serial-port-enable"     = "false"
    "user-data"              = local.cloud_init
  }
  lifecycle {
    precondition {
      condition     = startswith(var.zone, "${var.region}-")
      error_message = "The explicit zone must belong to the explicit region."
    }
  }
}

# https://docs.cloud.google.com/firewall/docs/firewalls#alwaysallowed
# Single app node; no cross-node overlay/API ports. Google metadata (DNS/NTP)
# is always reachable by the trusted host regardless of VPC firewall rules.
# Tenant metadata/private-network denial remains a separate Cilium policy.
locals {
  host_egress = {
    profile            = var.host_egress_profile
    https_ports        = ["443"]
    metadata_exception = "google-platform-always-allowed"
  }
}
resource "google_compute_firewall" "host_https" {
  name                    = "${var.name}-host-https"
  network                 = google_compute_network.node.name
  direction               = "EGRESS"
  priority                = 1000
  destination_ranges      = ["0.0.0.0/0"]
  target_service_accounts = [google_service_account.node.email]
  allow {
    protocol = "tcp"
    ports    = local.host_egress.https_ports
  }
}
resource "google_compute_firewall" "host_deny_other" {
  name                    = "${var.name}-host-deny-other"
  network                 = google_compute_network.node.name
  direction               = "EGRESS"
  priority                = 2000
  destination_ranges      = ["0.0.0.0/0"]
  target_service_accounts = [google_service_account.node.email]
  deny { protocol = "all" }
  log_config { metadata = "EXCLUDE_ALL_METADATA" }
}
