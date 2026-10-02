"""Render and parse the exact cloud-init; compile embedded code without executing it."""
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml


class BootstrapTests(unittest.TestCase):
    def test_pinned_public_source_ssm_only_and_absolute_deadline(self):
        here = Path(__file__).resolve().parent
        variables = {'platform_ref': 'a'*40, 'archive_sha256': 'b'*64,
                     'public_key': 'ssh-ed25519 AAAATEST', 'stop_at': '2030-01-01T00:00:00Z'}
        expression = 'base64encode(templatefile(' + json.dumps(str(here/'cloud-init.yaml.tftpl')) + ',' + json.dumps(variables) + '))'
        with tempfile.TemporaryDirectory() as tmp:
            process = subprocess.run(['terraform','console'],cwd=tmp,input=expression+'\n',text=True,capture_output=True,check=True)
        config = yaml.safe_load(base64.b64decode(json.loads(process.stdout)))
        boot = config['bootcmd'][0]
        code = boot.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
        compile(code,'cloud-init bootcmd','exec')
        self.assertIn("['systemctl','start','--no-block'",code)
        self.assertIn('datetime.now(timezone.utc) >= deadline',code)
        self.assertNotIn('enable --now',boot)
        script = next(f['content'] for f in config['write_files'] if f['path']=='/usr/local/sbin/railshot-ci-bootstrap')
        subprocess.run(['bash','-n'],input=script,text=True,check=True)
        self.assertIn('sha256sum --check --status',script)
        self.assertIn('https://codeload.github.com/Jasmin-Softbank/Railshot/tar.gz/' + variables['platform_ref'], script)
        self.assertIn('/infrastructure/ansible/ci.yml',script)
        self.assertIn('DOCKER_CONFIG=/var/lib/railshot-console/docker-config',script)
        self.assertNotIn('platform/control',json.dumps(config))
        self.assertNotIn('auth.json',json.dumps(config))
        self.assertNotIn('CODEX',json.dumps(config))
        policy=(here/'main.tf').read_text()
        self.assertIn('default     = null',policy)
        self.assertIn('ingress = [for rule in local.build_peer_health',policy)
        self.assertIn('security_groups  = [var.control_security_group_id]',policy)
        self.assertIn('"ssm:GetParameter", "ssm:GetParameters"',policy)
        self.assertIn('Effect = "Deny"',policy)
        self.assertIn('delete_on_termination = false',policy)
        self.assertIn('http_put_response_hop_limit = 1',policy)


if __name__=='__main__':unittest.main()
