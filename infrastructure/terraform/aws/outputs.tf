output "public_ip" { value = aws_eip.node.public_ip }
output "app_domain" { value = "${replace(aws_eip.node.public_ip, ".", "-")}.sslip.io" }
output "instance_id" { value = aws_instance.node.id }
output "ssm_session" { value = "aws ssm start-session --region ${var.region} --target ${aws_instance.node.id}" }
output "ci_plan_role_arn" { value = aws_iam_role.ci_plan.arn }

output "node_descriptor" {
  description = "Configured references only; no guest/readiness observation."
  value = {
    schema_version = "v1", provider_kind = "aws", target_id = var.target_id,
    owner_ref      = var.owner_ref, execution_driver = "terraform", resource_id = aws_instance.node.arn,
    instance_id    = aws_instance.node.id, compute = { machine_type = var.instance_type, source = "configured" },
    location       = { account_id = var.account_id, region = var.region, zone = data.aws_subnet.selected.availability_zone },
    architecture   = "x86_64", image_ref = var.ami_id,
    addresses      = { private = aws_instance.node.private_ip, public = aws_eip.node.public_ip },
    transport_ref  = "ssm:${var.region}:${aws_instance.node.id}",
    data_disk      = { resource_id = aws_ebs_volume.data.id, size_gib = var.data_disk_gib, mount_path = "/var/lib/rancher", preservation = "retain" },
    bootstrap      = { profile = "ubuntu2404-nitro-host-v1", revision = null, method = "host-preparation-only", status = "unverified" },
    gitops         = { repo = null, path = null, revision = null },
    runtime        = { configuration_status = "not_configured", readiness = "not_configured" }
  }
}
