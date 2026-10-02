"""Execute the actual host guard and policy parser with no host/root mutations."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[3]
TASKS = yaml.safe_load((ROOT / "infrastructure/ansible/ci.yml").read_text())[0]["tasks"]
GUARD = next(task["ansible.builtin.command"]["argv"][-1] for task in TASKS
             if "RAILSHOT_ALLOW_K3S_BUILD_WORKER" in task.get("environment", {}))
INSTALLER = next(task["ansible.builtin.copy"]["content"] for task in TASKS
                 if task.get("ansible.builtin.copy", {}).get("dest") == "/usr/local/sbin/railshot-ci-network")
POLICY = INSTALLER.split("<<'PYRAILSHOT_POLICY'\n", 1)[1].split("\nPYRAILSHOT_POLICY", 1)[0]
BUILD_CONFIG = {"node-label": ["railshot.io/node-role=build"],
                "node-taint": ["railshot.io/dedicated=build:NoSchedule"], "docker": False}


class HostPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Only root ownership and systemd state are mocked. File types, modes,
        # symlinks and the config parser are exercised by the real shell guard.
        self.executable("stat", "import os, stat, sys\n"
                        "print(os.environ.get('TEST_OWNER', '0') + ':' + "
                        "format(stat.S_IMODE(os.stat(sys.argv[-1]).st_mode), 'o'))\n")
        self.executable("systemctl", "import os, sys\n"
                        "sys.exit(0 if sys.argv[-1] in os.environ.get('TEST_ACTIVE', 'k3s-agent').split(',') else 3)\n")

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o755)

    def path(self, path):
        return self.root / path.lstrip("/")

    def agent(self, config=None):
        self.path("/var/lib/rancher/k3s/agent").mkdir(parents=True, exist_ok=True)
        target = self.path("/etc/rancher/k3s/config.yaml")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(BUILD_CONFIG if config is None else config))
        target.chmod(0o600)
        return target

    def run_guard(self, approved=False, **env):
        source = GUARD.replace("/var/lib/", str(self.root) + "/var/lib/")
        source = source.replace("/etc/", str(self.root) + "/etc/")
        return subprocess.run(["bash", "-c", source], capture_output=True, text=True, env={
            **os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "RAILSHOT_ALLOW_K3S_BUILD_WORKER": "true" if approved else "false",
            "RAILSHOT_ANSIBLE_PYTHON": sys.executable, **env}).returncode

    def test_standalone_default_and_explicit_build_agent(self):
        self.assertEqual(self.run_guard(), 0)
        self.assertNotEqual(self.run_guard(approved=True), 0)
        self.agent()
        self.assertNotEqual(self.run_guard(), 0)
        self.assertEqual(self.run_guard(approved=True), 0)
        self.assertNotEqual(self.run_guard(approved=True, TEST_ACTIVE=""), 0)
        for service in ("k3s", "kubelet"):
            with self.subTest(service=service):
                self.assertNotEqual(self.run_guard(approved=True, TEST_ACTIVE="k3s-agent," + service), 0)

    def test_control_plane_and_standard_kube_markers_are_refused(self):
        self.agent()
        for name in ("/var/lib/rancher/k3s/server", "/etc/rancher/k3s/k3s.yaml", "/etc/kubernetes"):
            with self.subTest(path=name):
                marker = self.path(name)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.symlink_to(self.root / "missing")
                self.assertNotEqual(self.run_guard(approved=True), 0)
                marker.unlink()

    def test_ordinary_agent_and_ambiguous_or_docker_configs_are_refused(self):
        bad = [ {}, {**BUILD_CONFIG, "node-label": "railshot.io/node-role=build"},
                {**BUILD_CONFIG, "node-taint": []},
                {**BUILD_CONFIG, "node-label": BUILD_CONFIG["node-label"] + ["railshot.io/node-role=ops"]},
                {**BUILD_CONFIG, "node-taint+": ["other=value:NoSchedule"]} ]
        bad.extend({**BUILD_CONFIG, "docker": value} for value in (True, "false", 0, None))
        for config in bad:
            with self.subTest(config=config):
                self.agent(config)
                self.assertNotEqual(self.run_guard(approved=True), 0)

    def test_config_ownership_mode_symlink_and_dropins_are_refused(self):
        config = self.agent()
        self.assertNotEqual(self.run_guard(approved=True, TEST_OWNER="1000"), 0)
        config.chmod(0o644)
        self.assertNotEqual(self.run_guard(approved=True), 0)
        config.chmod(0o600)
        real = config.with_suffix(".real")
        config.rename(real)
        config.symlink_to(real)
        self.assertNotEqual(self.run_guard(approved=True), 0)
        config.unlink()
        real.rename(config)
        dropins = config.with_name("config.yaml.d")
        dropins.mkdir()
        self.assertNotEqual(self.run_guard(approved=True), 0)
        dropins.rmdir()
        dropins.symlink_to(self.root / "missing")
        self.assertNotEqual(self.run_guard(approved=True), 0)


class NetworkPolicyTest(unittest.TestCase):
    def setUp(self):
        self.rules = {
            ("iptables", "FORWARD"): ["-A FORWARD -j DOCKER-USER"],
            ("iptables", "DOCKER-USER"): ["-A DOCKER-USER -i br-railshot -j RAILSHOT-CI-EGRESS"],
            ("iptables", "INPUT"): [
                "-A INPUT -i rsrun+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
                "-A INPUT -i rsrun+ -m comment --comment railshot-runtime-host-block -j DROP",
                "-A INPUT -i br-railshot -m comment --comment railshot-ci-host-block -j DROP"],
            ("ip6tables", "INPUT"): ["-A INPUT -i rsrun+ -j DROP", "-A INPUT -i br-railshot -j DROP"],
            ("ip6tables", "FORWARD"): ["-A FORWARD -i rsrun+ -j DROP", "-A FORWARD -i br-railshot -j DROP"],
            ("iptables", "RAILSHOT-CI-EGRESS"): ["-A RAILSHOT-CI-EGRESS -d 10.0.0.0/8 -j DROP",
                                                   "-A RAILSHOT-CI-EGRESS -j DROP"],
        }

    def policy_digest(self, rules):
        def output(command, **kwargs):
            self.assertEqual(command[1:4], ["-w", "10", "-S"])
            return "\n".join(rules[(command[0], command[-1])]) + "\n"
        with patch("subprocess.check_output", side_effect=output), contextlib.redirect_stdout(io.StringIO()) as stream:
            exec(compile(POLICY, "ci.yml:policy", "exec"), {})
        # This serialized policy is an input to the installed helper fingerprint.
        json.loads(stream.getvalue())
        return hashlib.sha256(stream.getvalue().encode()).hexdigest()

    def test_cni_rules_after_hooks_do_not_invalidate_policy(self):
        baseline = self.policy_digest(self.rules)
        for key in self.rules:
            if key[1] != "RAILSHOT-CI-EGRESS":
                self.rules[key].append("-A " + key[1] + " -j CILIUM_FORWARD")
        self.assertEqual(baseline, self.policy_digest(self.rules))

    def test_accept_before_any_hook_fails_closed(self):
        for key in self.rules:
            if key[1] == "RAILSHOT-CI-EGRESS":
                continue
            with self.subTest(chain=key):
                rules = copy.deepcopy(self.rules)
                rules[key].insert(0, "-A " + key[1] + " -j ACCEPT")
                with self.assertRaises(SystemExit):
                    self.policy_digest(rules)

    def test_owned_policy_tamper_changes_fingerprint_and_final_drop_is_required(self):
        baseline = self.policy_digest(self.rules)
        egress = self.rules[("iptables", "RAILSHOT-CI-EGRESS")]
        egress[0] = egress[0].replace("DROP", "ACCEPT")
        self.assertNotEqual(baseline, self.policy_digest(self.rules))
        egress[-1] = egress[-1].replace("DROP", "RETURN")
        with self.assertRaises(SystemExit):
            self.policy_digest(self.rules)


if __name__ == "__main__":
    unittest.main()
