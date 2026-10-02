"""CNI refusal/profile regressions without root, downloads or a Kubernetes cluster."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('cilium_preflight', ROOT / 'deployment/cilium/preflight.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
CONTROL = (ROOT / 'infrastructure/ansible/control.sh').read_text()
CONFIG = CONTROL.split('<<\'YAML\'\n', 1)[1].split('\nYAML', 1)[0] + '\n'


class ControlCiliumTests(unittest.TestCase):
    def host(self, root, config=CONFIG, role='control'):
        for name, content in [('etc/rancher/k3s/config.yaml', config), ('etc/railshot/node-role', role)]:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def test_profiles_and_foreign_cni_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.host(root)
            podman = root / 'etc/cni/net.d/87-podman-bridge.conflist'
            podman.parent.mkdir(parents=True)
            podman.write_text('{"name":"podman", "plugins":[{"type":"bridge"}]}')
            with self.assertRaisesRegex(ValueError, 'non-Cilium'):
                guard.check_host('control', root)
            podman.unlink()
            self.assertEqual(guard.check_host('control', root), ('10.52.0.0/16', '10.53.0.0/16'))
            with self.assertRaisesRegex(ValueError, 'role differs'):
                guard.check_host('customer', root)
            for bad in (CONFIG.replace('flannel-backend: none\n', ''),
                        CONFIG.replace('10.52.', '10.42.'), CONFIG + 'cluster-cidr: 10.42.0.0/16\n',
                        CONFIG + 'disable-kube-proxy: true\n', CONFIG + '"cluster-cidr": 10.42.0.0/16\n',
                        CONFIG + '<<: *alternate\n'):
                self.host(root, bad)
                with self.assertRaises(ValueError):
                    guard.check_host('control', root)
            self.host(root)
            cni = root / 'var/lib/rancher/k3s/agent/etc/cni/net.d/10-cni.conflist'
            cni.parent.mkdir(parents=True)
            cni.write_text(json.dumps({'name': 'cbr0', 'plugins': [{'type': 'flannel'}]}))
            with self.assertRaisesRegex(ValueError, 'non-Cilium'):
                guard.check_host('control', root)
            cni.write_text(json.dumps({'name': 'cilium', 'plugins': [{'type': 'cilium-cni'}]}))
            guard.check_host('control', root)
            link = root / 'sys/class/net/flannel.1'
            link.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'flannel.1'):
                guard.check_host('control', root)
            link.rmdir()
            self.host(root, CONFIG.replace('10.52.', '10.42.').replace('10.53.', '10.43.'), 'customer')
            self.assertEqual(guard.check_host('customer', root), ('10.42.0.0/16', '10.43.0.0/16'))

    def test_cluster_profile_and_api_errors_fail_closed(self):
        nodes = {'items': [{'spec': {'podCIDR': '10.52.0.0/24'}}]}
        service = {'spec': {'clusterIP': '10.53.0.1'}}
        config = {'metadata': {'annotations': {'meta.helm.sh/release-name': 'cilium'}}, 'data': {
            'cluster-pool-ipv4-cidr': '10.52.0.0/16', 'cluster-pool-ipv4-mask-size': '24',
            'ipam': 'cluster-pool', 'kube-proxy-replacement': 'false',
            'routing-mode': 'tunnel', 'tunnel-protocol': 'vxlan', 'enable-hubble': 'false'}}
        pinned = json.loads((ROOT / 'deployment/airgap/versions.json').read_text())['cilium_images']['agent']
        daemon = {'spec': {'template': {'spec': {'containers': [{'name': 'cilium-agent', 'image': pinned}]}}}}
        for existing in (False, True):
            with patch.object(guard, 'kube', side_effect=[nodes, service, config if existing else None, daemon if existing else None]):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16')
        with patch.object(guard, 'kube', side_effect=[{'items': []}]):
            with self.assertRaisesRegex(ValueError, 'registration/PodCIDR'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', wait_seconds=0)
        with patch.object(guard, 'kube', side_effect=[{'items': nodes['items'] * 2}]):
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16')
        with patch.object(guard, 'kube', side_effect=[{'items': []}, {'items': [{'spec': {}}]}, nodes, service, None, None]), patch.object(guard.time, 'sleep') as sleep:
            guard.check_cluster('10.52.0.0/16', '10.53.0.0/16')
            self.assertEqual(sleep.call_count, 2)
        with patch.object(guard, 'kube', side_effect=[nodes, service]):
            with self.assertRaisesRegex(ValueError, 'Service CIDR'):
                guard.check_cluster('10.52.0.0/16', '10.43.0.0/16')
        config['data']['cluster-pool-ipv4-cidr'] = '10.42.0.0/16'
        with patch.object(guard, 'kube', side_effect=[nodes, service, config]):
            with self.assertRaisesRegex(ValueError, 'implicit migration'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16')
        config['data']['cluster-pool-ipv4-cidr'] = '10.52.0.0/16'
        daemon['spec']['template']['spec']['containers'][0]['image'] = 'quay.io/cilium/cilium:other'
        with patch.object(guard, 'kube', side_effect=[nodes, service, config, daemon]):
            with self.assertRaisesRegex(ValueError, 'explicit upgrade'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16')
        with patch.object(guard, 'kube', side_effect=subprocess.CalledProcessError(1, ['kubectl'])):
            with self.assertRaises(subprocess.CalledProcessError):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16')

    def test_flannel_control_refuses_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = CONFIG.replace('flannel-backend: none\n', '').replace('disable-network-policy: true\n', '')
            self.host(root, legacy)
            (root / 'run').mkdir()
            binaries = root / 'bin'
            binaries.mkdir()
            for name, body in {'id': 'echo 0', 'uname': 'if [ "$1" = -s ]; then echo Linux; else echo x86_64; fi',
                               'flock': 'exit 0', 'install': 'exit 99', 'curl': 'exit 99', 'systemctl': 'exit 99'}.items():
                path = binaries / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            script = CONTROL
            for path in ('/etc/', '/var/lib/', '/run/'):
                script = script.replace(path, str(root) + path)
            entry = root / 'control.sh'
            entry.write_text(script)
            env = {k: v for k, v in os.environ.items() if not k.startswith(('K3S_', 'INSTALL_K3S_', 'CILIUM_', 'RAILSHOT_', 'NODE_IP'))}
            env['PATH'] = str(binaries) + ':' + env['PATH']
            result = subprocess.run(['bash', str(entry)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('Automatic CNI migration is refused', result.stderr)
            self.assertEqual((root / 'etc/rancher/k3s/config.yaml').read_text(), legacy)
            self.assertEqual((root / 'etc/railshot/node-role').read_text(), 'control')


if __name__ == '__main__':
    unittest.main()
