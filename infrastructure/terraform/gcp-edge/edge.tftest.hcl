mock_provider "google" {
  mock_data "google_compute_instance" {
    defaults = {
      name = "railshot-gcp-poc"
      network_interface = [{
        network_ip = "10.66.0.2"
        network    = "projects/railshot-poc-20261001/global/networks/railshot-gcp-poc-vpc"
        subnetwork = "projects/railshot-poc-20261001/regions/asia-northeast3/subnetworks/railshot-gcp-poc-subnet"
      }]
      service_account = [{ email = "railshot-gcp-poc-node@railshot-poc-20261001.iam.gserviceaccount.com" }]
    }
  }
}

variables {
  project_id          = "railshot-poc-20261001"
  zone                = "asia-northeast3-a"
  instance_name       = "railshot-gcp-poc"
  expected_private_ip = "10.66.0.2"
  hostname            = "fixture-npm-js-3feba5a1b1cf.railshot.io"
}

run "existing_vm_gfe_only" {
  command = plan
  assert {
    condition     = google_compute_backend_service.app.load_balancing_scheme == "EXTERNAL_MANAGED" && google_compute_network_endpoint.app.ip_address == "10.66.0.2" && google_compute_network_endpoint.app.port == 30080 && google_compute_network_endpoint_group.app.network_endpoint_type == "GCE_VM_IP_PORT"
    error_message = "Use the existing private NodePort through a native global managed LB."
  }
  assert {
    condition     = google_compute_firewall.gfe.source_ranges == toset(["35.191.0.0/16", "130.211.0.0/22"]) && one(google_compute_firewall.gfe.allow).ports == tolist(["30080"]) && google_compute_firewall.gfe.target_service_accounts == toset(["railshot-gcp-poc-node@railshot-poc-20261001.iam.gserviceaccount.com"])
    error_message = "Only GFE sources may enter this VM service account on the allocated NodePort."
  }
}

run "wrong_vm_ip_rejected" {
  command = plan
  variables { expected_private_ip = "10.66.0.3" }
  expect_failures = [google_compute_network_endpoint_group.app]
}
