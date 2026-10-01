"""Offline render checks: Terraform + PyYAML; no credentials, API, VM, or shell execution.

Run directly: python3 test_bootstrap.py; evaluates a fresh source-only directory.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


MODULE = Path(__file__).resolve().parent
def evaluate(expression, overrides=None):
    inputs = {
        "project_id": "railshot-offline-test",
        "region": "asia-northeast3", "zone": "asia-northeast3-a",
        "target_id": "gcp-offline-test", "owner_ref": "terraform:offline:gcp",
        # Syntactic fixture, not a claim that this image exists.
        "boot_image": "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20000101",
    }
    inputs.update(overrides or {})
    # Never run console against a module directory that could hold real state/tfvars.
    with tempfile.TemporaryDirectory(prefix="railshot-gcp-render-") as directory:
        work = Path(directory)
        for name in ("variables.tf", "cloud-init.yaml.tftpl", "bootstrap.sh.tftpl"):
            (work / name).write_text((MODULE / name).read_text())
        pure_locals = (MODULE / "main.tf").read_text().split('resource "google_compute_network"', 1)[0]
        (work / "locals.tf").write_text(pure_locals)
        fixture = work / "fixture.tfvars.json"
        fixture.write_text(json.dumps(inputs))
        result = subprocess.run(
            ["terraform", "console", "-no-color", "-var-file=" + str(fixture)],
            input="jsonencode(" + expression + ")\n", text=True, capture_output=True,
            check=True, cwd=work,
        )
    # Terraform 1.5 console can return status 0 despite input-validation diagnostics.
    if "Error:" in result.stderr:
        raise ValueError(result.stderr)
    return json.loads(json.loads(result.stdout))


def render():
    return yaml.safe_load(evaluate("local.cloud_init"))


class BootstrapRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.git = render()

    def file(self, config, path):
        return next(item for item in config["write_files"] if item["path"] == path)

    def test_host_only_script_parses_without_execution(self):
        script = self.file(self.git, "/usr/local/sbin/railshot-bootstrap")["content"]
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        for retired in ("ansible", "argocd", "get.k3s.io", "gitops"):
            self.assertNotIn(retired, json.dumps(self.git))
        self.assertFalse(any('/systemd/system/k3s' in item['path'] for item in self.git['write_files']))

    def test_cloud_config_contains_only_host_handoff(self):
        node = yaml.safe_load(self.file(self.git, "/etc/railshot/host.yml")["content"])
        self.assertEqual(node["cloud_provider"], "gcp")
        self.assertEqual(node["runtime_status"], "not_configured")
        self.assertEqual(set(node), {"name", "node_name", "region", "cloud_provider", "runtime_status"})

    def test_data_mount_is_required_before_host_completion(self):
        script = self.file(self.git, "/usr/local/sbin/railshot-bootstrap")["content"]
        self.assertLess(script.index("mounted_uuid="), script.index("host_prepared"))
        self.assertIn("[ 'false' = true ]", script)
        self.assertIn("existing non-ext4 filesystem; refusing format", script)
        self.assertIn('runtime_ready":"not_configured', script)
        self.assertNotIn("mkfs.ext4 -F", script)

    def test_optional_iap_and_runtime_limit_are_off_by_default(self):
        policy = evaluate("{iap = local.iap_ssh, runtime = local.runtime_limit}")
        self.assertEqual(policy, {"iap": None, "runtime": None})

    def test_registry_oauth_scope_is_explicit_and_read_only(self):
        self.assertEqual(evaluate("local.node_oauth_scopes"), [])
        self.assertEqual(evaluate("local.node_oauth_scopes", overrides={"enable_gcp_registry_pull": True}),
                         ["https://www.googleapis.com/auth/devstorage.read_only"])

    def test_opt_in_uses_only_iap_ssh_and_stops_without_restart(self):
        policy = evaluate(
            "{iap = local.iap_ssh, runtime = local.runtime_limit}",
            overrides={"allow_iap_ssh": True, "max_run_duration_seconds": 7200},
        )
        self.assertEqual(policy["iap"]["source_ranges"], ["35.235.240.0/20"])
        self.assertEqual(policy["iap"]["ports"], ["22"])
        self.assertEqual(policy["iap"]["transport_ref"], "iap:railshot-offline-test/asia-northeast3-a/railshot-gcp")
        self.assertEqual(policy["runtime"], {
            "seconds": 7200, "automatic_restart": False, "instance_termination_action": "STOP",
        })

    def test_node_identity_is_explicit_and_existing_fqdn_can_be_preserved(self):
        config = yaml.safe_load(self.file(self.git, "/etc/railshot/host.yml")["content"])
        self.assertEqual(config["node_name"], "railshot-gcp")
        original = "railshot-gcp-poc.asia-northeast3-a.c.example.internal"
        rendered = yaml.safe_load(evaluate("local.cloud_init", overrides={"node_name": original}))
        self.assertEqual(yaml.safe_load(self.file(rendered, "/etc/railshot/host.yml")["content"])["node_name"], original)
        for invalid in ("UPPER.invalid", "node..invalid", "node/other", "a" * 64):
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                evaluate("local.cloud_init", overrides={"node_name": invalid})

    def test_invalid_runtime_limits_are_rejected_by_terraform(self):
        for duration in (29, 10368001, 30.5):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ValueError, "max_run_duration_seconds must be null or an integer"):
                    evaluate("local.runtime_limit", overrides={"max_run_duration_seconds": duration})


if __name__ == "__main__":
    unittest.main(verbosity=2)
