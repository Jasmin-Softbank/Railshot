# Mock providers exercise the rendered IAM/credential contract without cloud writes.
mock_provider "aws" {
  mock_resource "aws_ssm_document" {
    defaults = { hash_type = "Sha256" }
  }
}

mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "359201781699" }
  }
  mock_data "google_compute_instance" {
    defaults = {
      network_interface = [{ network = "projects/railshot-poc-20261001/global/networks/railshot-runtime" }]
      service_account   = [{ email = "runtime@railshot-poc-20261001.iam.gserviceaccount.com" }]
    }
  }
  mock_resource "google_iam_workload_identity_pool" {
    defaults = { name = "projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release" }
  }
  mock_resource "google_iam_workload_identity_pool_provider" {
    defaults = { name = "projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release/providers/aws-control" }
  }
  mock_resource "google_service_account" {
    defaults = {
      email = "railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com"
      name  = "projects/railshot-poc-20261001/serviceAccounts/railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com"
    }
  }
}

run "exact_instance_and_read_update_authority" {
  command = apply

  assert {
    condition = (
      google_iam_workload_identity_pool_provider.release.aws[0].account_id == "721622471953" &&
      google_iam_workload_identity_pool_provider.release.attribute_mapping["google.subject"] == "assertion.arn" &&
      google_iam_workload_identity_pool_provider.release.attribute_condition == "assertion.account == '721622471953' && assertion.arn == 'arn:aws:sts::721622471953:assumed-role/railshot-control-poc/i-033ae2db907fde68e'" &&
      google_service_account_iam_member.control_federation.member == "principal://iam.googleapis.com/projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release/subject/arn:aws:sts::721622471953:assumed-role/railshot-control-poc/i-033ae2db907fde68e" &&
      google_service_account_iam_member.control_federation.role == "roles/iam.workloadIdentityUser"
    )
    error_message = "Only the exact AWS account, role and EC2 session may impersonate the release service account."
  }

  assert {
    condition = (
      google_project_iam_member.edge_release.project == "railshot-poc-20261001" &&
      google_project_iam_member.edge_release.member == "serviceAccount:railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com" &&
      alltrue([for permission in google_project_iam_custom_role.edge_release.permissions :
        can(regex("^(compute|certificatemanager|resourcemanager|serviceusage)\\.[A-Za-z]+\\.(get|list|use|useReadOnly|update|setLabels|setTarget|setUrlMap|setCertificateMap)$", permission))
      ]) &&
      !contains(google_project_iam_custom_role.edge_release.permissions, "compute.instances.update") &&
      !contains(google_project_iam_custom_role.edge_release.permissions, "compute.disks.update") &&
      contains(google_project_iam_custom_role.edge_release.permissions, "compute.globalOperations.get") &&
      contains(google_project_iam_custom_role.edge_release.permissions, "certificatemanager.operations.get")
    )
    error_message = "The executor must not gain creation/deletion, IAM, VM modification, key publication, or API enable permissions."
  }

  assert {
    condition = (
      google_iap_tunnel_instance_iam_member.runtime_release.project == "railshot-poc-20261001" &&
      google_iap_tunnel_instance_iam_member.runtime_release.zone == "asia-northeast3-a" &&
      google_iap_tunnel_instance_iam_member.runtime_release.instance == "railshot-gcp-poc" &&
      google_iap_tunnel_instance_iam_member.runtime_release.role == "roles/iap.tunnelResourceAccessor" &&
      google_iap_tunnel_instance_iam_member.runtime_release.member == google_project_iam_member.edge_release.member &&
      google_iap_tunnel_instance_iam_member.runtime_release.condition[0].expression == "destination.port == 22"
    )
    error_message = "IAP tunnel access must be limited to SSH on the one existing runtime VM."
  }

  assert {
    condition = (
      google_compute_firewall.release_observer.network == "projects/railshot-poc-20261001/global/networks/railshot-runtime" &&
      google_compute_firewall.release_observer.source_ranges == toset(["52.78.97.236/32"]) &&
      google_compute_firewall.release_observer.target_service_accounts == toset(["runtime@railshot-poc-20261001.iam.gserviceaccount.com"]) &&
      one(google_compute_firewall.release_observer.allow).protocol == "tcp" &&
      toset(one(google_compute_firewall.release_observer.allow).ports) == toset(["31490", "31491"]) &&
      alltrue([for port, rule in aws_vpc_security_group_ingress_rule.release_observer :
        rule.security_group_id == "sg-0ad2a18168c4eac2b" &&
        rule.referenced_security_group_id == "sg-01a71e8be9a585b5a" &&
        rule.ip_protocol == "tcp" && rule.from_port == tonumber(port) && rule.to_port == tonumber(port)
      ])
    )
    error_message = "Observer bootstrap must only open the two metric ports from the existing control source."
  }

  assert {
    condition = (
      jsondecode(output.gcp_external_account_json).type == "external_account" &&
      jsondecode(output.gcp_external_account_json).audience == "//iam.googleapis.com/projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release/providers/aws-control" &&
      jsondecode(output.gcp_external_account_json).service_account_impersonation_url == "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com:generateAccessToken" &&
      jsondecode(output.gcp_external_account_json).credential_source.imdsv2_session_token_url == "http://169.254.169.254/latest/api/token" &&
      !can(jsondecode(output.gcp_external_account_json).private_key) &&
      !can(jsondecode(output.gcp_external_account_json).refresh_token)
    )
    error_message = "The host credential file must be keyless AWS IMDSv2 configuration for the exact audience and service account."
  }
}

run "wrong_project_number_rejected" {
  command = plan
  override_data {
    target = data.google_project.release
    values = { number = "359201781698" }
  }
  expect_failures = [data.google_project.release]
}
