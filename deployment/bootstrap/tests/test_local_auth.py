import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/bootstrap'))
sys.path.insert(0, str(ROOT))

from client_setup.main import entered_auth, run_runtime
from client_setup.preflight import parse_args, validate_config
from infrastructure.providers.openstack.cli import OpenStackCLI


class LocalAuthTest(unittest.TestCase):
    def setUp(self):
        self.config = {'openstack': {'auth_url': 'https://keystone.example.test/v3',
                                     'project_id': 'project-id', 'user_id': 'user-id'},
                       'vm_access': {'network_id': 'network-id', 'ssh_username': 'ubuntu',
                                     'ssh_source_cidr': '192.0.2.10/32'}}

    def test_token_auth_stays_on_customer_node(self):
        validate_config(self.config)
        with patch('builtins.input', return_value='token'), patch('getpass.getpass', return_value='secret-token'):
            auth = entered_auth(self.config)
        self.assertEqual(auth['token'], 'secret-token')
        self.assertNotIn('token', json.dumps(self.config))
        def runner(command, **kwargs):
            cloud = json.loads(Path(kwargs['env']['OS_CLIENT_CONFIG_FILE']).read_text())['clouds']['railshot']
            self.assertEqual(cloud['auth_type'], 'v3token')
            self.assertEqual(cloud['auth']['token'], 'secret-token')
            self.assertNotIn('secret-token', command)
            return type('Result', (), {'returncode': 0, 'stdout': '{}', 'stderr': ''})()
        OpenStackCLI(auth, runner=runner).run(['token', 'issue'])

    def test_application_credential_and_companion_runtime(self):
        with patch('builtins.input', side_effect=['application_credential', 'credential-id']), \
             patch('getpass.getpass', return_value='credential-secret'):
            auth = entered_auth(self.config)
        self.assertEqual(auth['application_credential_id'], 'credential-id')
        self.assertEqual(auth['application_credential_secret'], 'credential-secret')
        args = parse_args(['init', '--project-id', 'project-id', '--user-id', 'user-id',
                           '--auth-type', 'application_credential', '--runtime-input', '/tmp/deployment.json'])
        self.assertEqual(args.auth_type, 'application_credential')
        self.assertEqual(args.runtime_input, Path('/tmp/deployment.json'))
        with patch('client_setup.main.subprocess.run') as execute:
            run_runtime(args.runtime_input)
        execute.assert_called_once_with(['bash', str(ROOT / 'deployment/scripts/deploy.sh'),
                                         '--input', str(Path('/tmp/deployment.json').resolve())], check=True)


if __name__ == '__main__':
    unittest.main()
