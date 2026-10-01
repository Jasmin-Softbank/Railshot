"""Render-only contract check; no provider configuration or guest execution."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


class BootstrapRenderTest(unittest.TestCase):
    def test_rendered_handoff_and_shell_syntax(self):
        module = Path(__file__).resolve().parents[1]
        terraform = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")
        self.assertIsNotNone(terraform, "terraform must be on PATH")
        config = {
            "name": "railshot-azure",
            "cloud_provider": "azure",
            "runtime_status": "not_configured",
        }
        inputs = {
            "host_config": yaml.safe_dump(config),
            "initialize_empty_data_disk": "false",
        }
        expression = (
            "jsonencode(yamldecode(templatefile("
            + json.dumps(str(module / "cloud-init.yaml.tftpl"))
            + ", "
            + json.dumps(inputs)
            + ")))\n"
        )
        with tempfile.TemporaryDirectory(prefix="railshot-azure-render-") as work:
            rendered = subprocess.run(
                [terraform, "console", "-no-color"],
                cwd=work,
                input=expression,
                text=True,
                capture_output=True,
                check=True,
            )
        cloud_config = json.loads(json.loads(rendered.stdout))
        files = {entry["path"]: entry for entry in cloud_config["write_files"]}
        node_file = files["/etc/railshot/host.yml"]
        self.assertEqual(yaml.safe_load(node_file["content"]), config)
        self.assertEqual(node_file["permissions"], "0600")
        script_path = "/usr/local/sbin/railshot-bootstrap"
        self.assertEqual(cloud_config["runcmd"], [[script_path]])
        script = files[script_path]["content"]
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        self.assertIn("test 'false' = true", script)
        self.assertIn("partitioned disk rejected", script)
        self.assertIn("unexpected disk signature; refusing format", script)
        self.assertIn('runtime_ready":"not_configured', script)
        self.assertLess(script.index('findmnt'), script.index('host_prepared'))
        for retired in ('ansible', 'argocd', 'get.k3s.io', 'gitops'):
            self.assertNotIn(retired, json.dumps(cloud_config))
        self.assertFalse(any('/systemd/system/k3s' in path for path in files))


if __name__ == "__main__":
    unittest.main()
