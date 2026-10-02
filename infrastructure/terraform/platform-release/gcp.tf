# Bootstrap authority installs federation; the release service account cannot
# change IAM, enable services, create/delete resources, or modify the backend VM.
locals {
  gcp_project_id     = "railshot-poc-20261001"
  gcp_project_number = "359201781699"
  aws_control_arn    = "arn:aws:sts::721622471953:assumed-role/railshot-control-poc/i-033ae2db907fde68e"
}

provider "google" {
  project = local.gcp_project_id
  region  = "asia-northeast3"
  zone    = "asia-northeast3-a"
}

data "google_project" "release" {
  project_id = local.gcp_project_id
  lifecycle {
    postcondition {
      condition     = tostring(self.number) == local.gcp_project_number
      error_message = "The WIF audience must belong to the reviewed GCP project number."
    }
  }
}

resource "google_project_service" "federation" {
  for_each = toset([
    "iam.googleapis.com", "iamcredentials.googleapis.com",
    "sts.googleapis.com", "cloudresourcemanager.googleapis.com", "iap.googleapis.com",
  ])
  project            = data.google_project.release.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_iam_workload_identity_pool" "release" {
  project                   = data.google_project.release.project_id
  workload_identity_pool_id = "railshot-aws-release"
  display_name              = "Railshot AWS control release"
  description               = "One existing AWS control instance updates the existing GCP edge."
  disabled                  = false
  depends_on                = [google_project_service.federation]
}

resource "google_iam_workload_identity_pool_provider" "release" {
  project                            = data.google_project.release.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.release.workload_identity_pool_id
  workload_identity_pool_provider_id = "aws-control"
  display_name                       = "Existing control instance"
  disabled                           = false
  aws { account_id = "721622471953" }
  attribute_mapping = {
    "google.subject"    = "assertion.arn"
    "attribute.account" = "assertion.account"
  }
  # Equality includes the instance-profile role AND the exact EC2 session name.
  attribute_condition = "assertion.account == '721622471953' && assertion.arn == '${local.aws_control_arn}'"
}

resource "google_service_account" "edge_release" {
  project      = data.google_project.release.project_id
  account_id   = "railshot-gcp-edge-release"
  display_name = "Railshot existing GCP edge release"
  description  = "Keyless AWS control-host federation; existing edge read and in-place updates only."
  depends_on   = [google_project_service.federation]
}

resource "google_service_account_iam_member" "control_federation" {
  service_account_id = google_service_account.edge_release.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.release.name}/subject/${local.aws_control_arn}"
}

# Ansible uses its existing operator SSH key through start-iap-tunnel; it does
# not publish keys to project/VM metadata or use OS Login.
resource "google_iap_tunnel_instance_iam_member" "runtime_release" {
  project  = data.google_project.release.project_id
  zone     = "asia-northeast3-a"
  instance = "railshot-gcp-poc"
  role     = "roles/iap.tunnelResourceAccessor"
  member   = "serviceAccount:${google_service_account.edge_release.email}"
  condition {
    title       = "operator-ssh-only"
    description = "Existing runtime VM SSH through IAP."
    expression  = "destination.port == 22"
  }
  depends_on = [google_project_service.federation]
}

# Observer connectivity is installed once by bootstrap authority. The release
# executor's gcp-edge state does not own this firewall or the existing VM.
data "google_compute_instance" "runtime" {
  project = data.google_project.release.project_id
  zone    = "asia-northeast3-a"
  name    = "railshot-gcp-poc"
}

resource "google_compute_firewall" "release_observer" {
  project                 = data.google_project.release.project_id
  name                    = "railshot-release-observer"
  network                 = one(data.google_compute_instance.runtime.network_interface).network
  direction               = "INGRESS"
  source_ranges           = ["52.78.97.236/32"]
  target_service_accounts = [one(data.google_compute_instance.runtime.service_account).email]
  description             = "Existing control observer reaches only the runtime metric NodePorts."
  allow {
    protocol = "tcp"
    ports    = ["31490", "31491"]
  }
}

resource "google_project_iam_custom_role" "edge_release" {
  project     = data.google_project.release.project_id
  role_id     = "railshotExistingEdgeRelease"
  title       = "Railshot existing edge release"
  description = "Refresh and in-place update operations used by the pinned gcp-edge module; no create/delete."
  permissions = [
    # google_project_service.Read checks project existence and lists enabled APIs.
    "resourcemanager.projects.get",
    "serviceusage.services.list",
    "serviceusage.services.use",
    # Backend data source expands boot-disk details; gcloud IAP requires get/list.
    "compute.instances.get",
    "compute.instances.list",
    "compute.disks.get",
    # Immutable NEG/endpoint resources: observe only; use permits backend binding.
    "compute.networkEndpointGroups.get",
    "compute.networkEndpointGroups.use",
    # Existing Compute edge resources and references used by their updates.
    "compute.firewalls.get",
    "compute.firewalls.update",
    "compute.healthChecks.get",
    "compute.healthChecks.update",
    "compute.healthChecks.useReadOnly",
    "compute.backendServices.get",
    "compute.backendServices.update",
    "compute.backendServices.use",
    "compute.urlMaps.get",
    "compute.urlMaps.update",
    "compute.urlMaps.use",
    "compute.targetHttpProxies.get",
    "compute.targetHttpProxies.setUrlMap",
    "compute.targetHttpProxies.use",
    "compute.targetHttpsProxies.get",
    "compute.targetHttpsProxies.setUrlMap",
    "compute.targetHttpsProxies.setCertificateMap",
    "compute.targetHttpsProxies.use",
    "compute.globalAddresses.get",
    "compute.globalAddresses.setLabels",
    "compute.globalForwardingRules.get",
    "compute.globalForwardingRules.setLabels",
    "compute.globalForwardingRules.setTarget",
    "compute.globalForwardingRules.update",
    "compute.globalOperations.get",
    # Certificate Manager PATCH operations, map references, and LRO polling.
    "certificatemanager.dnsauthorizations.get",
    "certificatemanager.dnsauthorizations.update",
    "certificatemanager.certs.get",
    "certificatemanager.certs.update",
    "certificatemanager.certs.use",
    "certificatemanager.certmaps.get",
    "certificatemanager.certmaps.update",
    "certificatemanager.certmaps.use",
    "certificatemanager.certmapentries.get",
    "certificatemanager.certmapentries.update",
    "certificatemanager.operations.get",
  ]
}

# IAM limits verbs and project; edge_update.py additionally binds the exact
# existing state lineage/resource IDs and rejects creation, replacement or drift.
resource "google_project_iam_member" "edge_release" {
  project = data.google_project.release.project_id
  role    = google_project_iam_custom_role.edge_release.name
  member  = "serviceAccount:${google_service_account.edge_release.email}"
}

output "gcp_release_identity" {
  value = {
    project_id      = local.gcp_project_id
    project_number  = local.gcp_project_number
    pool            = google_iam_workload_identity_pool.release.name
    provider        = google_iam_workload_identity_pool_provider.release.name
    service_account = google_service_account.edge_release.email
    aws_subject     = local.aws_control_arn
  }
}

# Public configuration, not a key or token. AIP-4117 AWS IMDSv2 credential shape;
# equivalent to gcloud create-cred-config --aws --enable-imdsv2 --service-account.
output "gcp_external_account_json" {
  value = jsonencode({
    universe_domain                   = "googleapis.com"
    type                              = "external_account"
    audience                          = "//iam.googleapis.com/${google_iam_workload_identity_pool_provider.release.name}"
    subject_token_type                = "urn:ietf:params:aws:token-type:aws4_request"
    token_url                         = "https://sts.googleapis.com/v1/token"
    service_account_impersonation_url = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${google_service_account.edge_release.email}:generateAccessToken"
    credential_source = {
      environment_id                 = "aws1"
      region_url                     = "http://169.254.169.254/latest/meta-data/placement/availability-zone"
      url                            = "http://169.254.169.254/latest/meta-data/iam/security-credentials"
      regional_cred_verification_url = "https://sts.{region}.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15"
      imdsv2_session_token_url       = "http://169.254.169.254/latest/api/token"
    }
  })
}
