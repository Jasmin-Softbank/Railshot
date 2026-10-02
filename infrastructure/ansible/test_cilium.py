"""Offline guards for the direct-inventory Cilium entrypoint; no remote execution."""
from pathlib import Path
import unittest

import yaml

HERE = Path(__file__).resolve().parent


class CiliumEntryTests(unittest.TestCase):
    def setUp(self):
        self.play = yaml.safe_load((HERE / 'site.yml').read_text())[0]
        self.defaults = yaml.safe_load((HERE / 'group_vars/all.yml').read_text())

    def test_cilium_runs_between_api_and_node_ready(self):
        names = [task['name'] for task in self.play['tasks']]
        api = names.index('Wait for local API readiness')
        cni = names.index('Install Cilium before waiting for Node Ready or CoreDNS')
        ready = names.index('Wait for node networking to become ready')
        self.assertLess(api, cni)
        self.assertLess(cni, ready)

    def test_legacy_identity_rejected_before_mutation(self):
        identity = self.play['vars']['k3s_identity']
        self.assertEqual(identity['cni'], 'cilium')
        self.assertIn('cilium_version', identity)
        self.assertIn('cilium_cli_version', identity)
        guard = next(t for t in self.play['pre_tasks'] if t['name'] == 'Reject implicit upgrades or destructive network changes')
        self.assertIn('(k3s_previous.content | b64decode | from_json) == k3s_identity',
                      guard['ansible.builtin.assert']['that'])
        self.assertEqual(guard['when'], 'k3s_existing.results[0].stat.exists')

    def test_reuses_team_installer_with_lock(self):
        tasks = self.play['tasks']
        copy = next(t for t in tasks if 'Reuse the team Cilium' in t['name'])
        self.assertEqual(copy['loop'], ['common.sh', 'install-cilium.sh'])
        install = next(t for t in tasks if t['name'].startswith('Install Cilium before'))
        self.assertEqual(install['ansible.builtin.command']['argv'][:3],
                         ['flock', '--nonblock', '/run/jasmin-deployment-poc.lock'])
        self.assertEqual(install['environment']['CILIUM_VERSION'], '{{ cilium_version }}')

    def test_pins_match_integrated_runtime(self):
        runtime = yaml.safe_load((HERE / 'runtime.yml').read_text())[0]
        identity = runtime['vars']['railshot_runtime_identity']
        self.assertEqual(self.defaults['k3s_version'], identity['k3s_version'])
        self.assertEqual(self.defaults['cilium_version'], identity['cilium_version'])
        profile = next(t for t in self.play['pre_tasks'] if t['name'] == 'Validate the shared single-node Cilium profile')
        checks = profile['ansible.builtin.assert']['that']
        self.assertIn("k3s_cluster_cidr == '10.42.0.0/16'", checks)
        self.assertIn("ansible_facts.architecture == 'x86_64'", checks)


if __name__ == '__main__':
    unittest.main()
