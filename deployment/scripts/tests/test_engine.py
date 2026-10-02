"""Ownership, failed-stage, cleanup and status tests without CSP resources."""
import json
import os
from pathlib import Path
import sys
import unittest
import tempfile
import time
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import DeploymentEngine, DeploymentError, CommandRunner
from input_adapter import parse_input
from render import labels, render, NAME
from test_contract import fixture


class Runner:
    def __init__(self,spec,foreign=False,fail=None):
        self.spec,self.foreign,self.fail,self.calls=spec,foreign,fail,[]
    def run(self,args,**kwargs):
        self.calls.append((args,kwargs))
        if self.fail and self.fail in ' '.join(args):
            raise DeploymentError('COMMAND_FAILED','ImagePullBackOff: tag does not exist',1)
        if 'kubectl' not in args: return ''
        if 'get' in args and 'namespace' in args:
            return json.dumps({'metadata':{'labels':{} if self.foreign else labels(self.spec)}})
        if 'get' in args and 'deployment' in args:
            return json.dumps(render(self.spec)['items'][1])
        if 'get' in args and ('service' in args or 'configmap' in args):
            if 'service' in args: return json.dumps(render(self.spec)['items'][2])
            return ''
        if 'get' in args and 'pods' in args: return '{"items":[]}'
        if 'get' in args and 'nodes' in args:
            return '{"items":[{"status":{"addresses":[{"type":"InternalIP","address":"127.0.0.1"}]}}]}'
        return ''


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.spec=parse_input(fixture()).spec
        self.open=patch('engine.open',return_value=MagicMock()); self.open.start()
        self.flock=patch('engine.fcntl.flock'); self.flock.start()
    def tearDown(self):
        self.flock.stop(); self.open.stop()

    def test_success_states_and_provider_independence(self):
        runner=Runner(self.spec); engine=DeploymentEngine(self.spec,runner)
        response=MagicMock(); response.__enter__.return_value.status=200
        response.__enter__.return_value.read.return_value=b'Railshot Runtime OK\n'
        with patch.object(engine,'network_check'),patch('engine.build_opener') as opener:
            opener.return_value.open.return_value=response
            result=engine.execute()
        self.assertEqual(result.status,'ready')
        self.assertEqual([e['state'] for e in result.states],['NODE_READY','K3S_INSTALLING','K3S_READY','CILIUM_INSTALLING','CILIUM_READY','WORKLOAD_DEPLOYING','WORKLOAD_READY','ENDPOINT_READY'])
        self.assertEqual(result.endpoint,'http://127.0.0.1:30080')
        self.assertNotIn('aws',' '.join(a for args,_ in runner.calls for a in args))
        self.assertTrue(any(k.get('input_text') for _,k in runner.calls))

    def test_foreign_namespace_is_never_overwritten(self):
        runner=Runner(self.spec,foreign=True)
        result=DeploymentEngine(self.spec,runner).execute()
        self.assertEqual(result.error['code'],'OWNERSHIP_CONFLICT')
        self.assertFalse(any('apply' in args for args,_ in runner.calls))

    def test_failed_rollout_reports_stage_and_diagnostic(self):
        result=DeploymentEngine(self.spec,Runner(self.spec,fail='rollout status')).execute()
        self.assertEqual(result.status,'failed')
        self.assertEqual(result.error['stage'],'WORKLOAD_DEPLOYING')
        self.assertEqual(result.error['exit_code'],1)
        self.assertIn('ImagePullBackOff',result.error['message'])
        self.assertEqual(result.states[-1]['state'],'FAILED')

    def test_full_cleanup_requires_explicit_flag_before_commands(self):
        runner=Runner(self.spec)
        result=DeploymentEngine(self.spec,runner).execute('cleanup',True,False)
        self.assertEqual(result.error['code'],'DESTRUCTIVE_ACTION_REQUIRES_FLAG')
        self.assertFalse(runner.calls)

    def test_cleanup_only_deletes_named_owned_resources(self):
        runner=Runner(self.spec)
        result=DeploymentEngine(self.spec,runner).execute('cleanup')
        self.assertEqual(result.status,'cleaned')
        deletes=[args for args,_ in runner.calls if 'delete' in args]
        self.assertEqual(len(deletes),1)
        self.assertIn('deployment,service,configmap',deletes[0])
        self.assertNotIn('namespace',deletes[0])

    def test_lock_contention_stops_before_mutation(self):
        runner=Runner(self.spec)
        with patch('engine.fcntl.flock',side_effect=BlockingIOError):
            result=DeploymentEngine(self.spec,runner).execute()
        self.assertEqual(result.error['code'],'RUNTIME_BUSY')
        self.assertEqual(len(runner.calls),1)

    def test_verify_does_not_install_or_apply(self):
        runner=Runner(self.spec); engine=DeploymentEngine(self.spec,runner)
        response=MagicMock(); response.__enter__.return_value.status=200
        response.__enter__.return_value.read.return_value=b'Railshot Runtime OK'
        with patch.object(engine,'network_check'),patch('engine.build_opener') as opener:
            opener.return_value.open.return_value=response
            result=engine.execute('verify')
        self.assertEqual(result.status,'ready')
        self.assertFalse(any('apply' in args or any('install' in s for s in args) for args,_ in runner.calls))

    def test_requested_image_drift_is_failure(self):
        runner=Runner(self.spec); engine=DeploymentEngine(self.spec,runner)
        self.spec.workload # immutable spec stays unchanged
        original=runner.run
        def drift(args,**kwargs):
            result=original(args,**kwargs)
            if 'get' in args and 'deployment' in args:
                data=json.loads(result); data['spec']['template']['spec']['containers'][0]['image']='nginx:other'
                return json.dumps(data)
            return result
        runner.run=drift
        result=engine.execute('verify')
        self.assertEqual(result.error['code'],'WORKLOAD_DRIFT')

    def test_timeout_stops_descendant_from_mutating_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'orphan-write'
            child=f'import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).write_text("orphan")'
            parent=f'import subprocess,time,sys; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)'
            with self.assertRaises(DeploymentError) as error:
                CommandRunner().run([sys.executable,'-c',parent],env=os.environ.copy(),timeout=0.2)
            self.assertEqual(error.exception.code,'COMMAND_TIMEOUT')
            time.sleep(1.2)
            self.assertFalse(marker.exists(),'timed-out child kept mutating after the command failed')


if __name__=='__main__': unittest.main()
