"""No-cloud lifecycle transactions: use real registration/control helpers and fake provider/runtime I/O."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import application_lifecycle as lifecycle
import applications
import environment as runtime
import test_applications


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_applications.ApplicationsTest(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.assertEqual(self.fixture.register()['status'], 'succeeded')
        self.app = self.fixture.request(); self.home = self.fixture.home()
        self.binding = runtime.read_private(self.home / 'binding.json')
        self.control = self.fixture.fixture.control
        self.calls = []
        native = self.control.__call__
        def control(namespace, *args, document=None):
            self.calls.append((args[0], args[1]))
            if args[:2] == ('delete', '--raw'):
                resource, key = args[2].split('/')[-2:]
                kind = {'applications': 'application', 'secrets': 'secret', 'appprojects': 'appproject'}[resource]
                item = self.control.objects[namespace, kind, key]
                self.assertEqual(document['preconditions'], {k: item['metadata'][k] for k in ('uid', 'resourceVersion')})
                del self.control.objects[namespace, kind, key]
                return {'kind': 'Status', 'status': 'Success'}
            if args[0] == 'patch':
                item = self.control.objects[namespace, args[1], args[2]]
                self.assertEqual(document[:2], [{'op': 'test', 'path': '/metadata/uid', 'value': item['metadata']['uid']},
                                               {'op': 'test', 'path': '/metadata/resourceVersion', 'value': item['metadata']['resourceVersion']}])
                item['metadata']['annotations'] = copy.deepcopy(document[2]['value'])
                return copy.deepcopy(item)
            return native(namespace, *args, document=document)
        self.enterContext(patch.object(runtime.argo, 'kubectl', lambda context, *args, **kwargs: control(*args, **kwargs)))
        app_id = self.app['application_id']; target = self.binding['registered']['target']
        app_name = runtime.application_name(app_id, app_id, self.app['app'])
        self.control.objects['argocd', 'application', app_name] = {
            'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Application',
            'metadata': {'name': app_name, 'labels': {'railshot.io/target': app_id, 'app.kubernetes.io/managed-by': 'railshot'}},
            'spec': {'project': app_id, 'source': {'repoURL': target['repo_url'], 'path': target['path']},
                     'destination': {'server': target['cluster_server'], 'namespace': app_id}}}
        for obj in self.control.objects.values():
            obj.setdefault('metadata', {}).setdefault('uid', str(uuid.uuid4()))
            obj['metadata']['resourceVersion'] = '1'
        self.inventory = {'version': 1, 'application_id': app_id, 'namespace': {'kind': 'Namespace', 'name': app_id, 'uid': 'namespace-1'},
                          'service_account': {'name': runtime.SA, 'namespace': app_id, 'uid': 'sa-1'},
                          'records': [], 'storage': [], 'resources': [{'kind': 'Namespace', 'name': app_id}], 'retained': []}
        self.enterContext(patch.object(lifecycle.workloads, 'inventory', side_effect=lambda *args: copy.deepcopy(self.inventory)))
        self.execute = self.enterContext(patch.object(lifecycle.workloads, 'execute', return_value={'status': 'succeeded', 'residuals': []}))
        self.edge_plan = self.enterContext(patch.object(lifecycle.edge, 'plan', return_value={'resources': [], 'private': {'id': 'owned-edge', 'health_path': '/health'}}))
        self.edge_validate = self.enterContext(patch.object(lifecycle.edge, 'validate', create=True))
        self.edge_execute = self.enterContext(patch.object(lifecycle.edge, 'execute', return_value={'status': 'succeeded'}))
        self.enterContext(patch.object(runtime.bridge, 'public_probe', return_value={'state': 'succeeded'}))
        self.calls.clear()

    def request(self, action='delete', **kwargs):
        return {'version': 1, 'phase': 'plan', **self.app, 'action': action, 'operation_id': str(uuid.uuid4()), **kwargs}

    def plan(self, action='delete'):
        return lifecycle.lifecycle(self.fixture.config_path, self.request(action))

    def apply(self, plan, action='delete', **kwargs):
        request = self.request(action, phase='apply', plan_id=plan['plan_id'], plan_hash=plan['plan_hash'], delete_data=action == 'delete', **kwargs)
        return request, lifecycle.lifecycle(self.fixture.config_path, request)

    def test_delete_preserves_other_app_and_replay_never_mutates_again(self):
        previous = copy.deepcopy(self.control.objects['argocd', 'configmap', 'railshot-credentials'])
        plan = self.plan()
        self.assertTrue(all(verb == 'get' for verb, _ in self.calls))
        request, result = self.apply(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['residuals'], [])
        app_id = self.app['application_id']
        self.assertFalse(any(key[2] == 'railshot-' + app_id for key in self.control.objects))
        policy = json.loads(self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        self.assertEqual(policy['targets'], [t for t in json.loads(previous['data']['policy.json'])['targets'] if t['target_id'] != app_id])
        self.assertEqual(self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['kubeconfig'], 'preserve-me')
        self.assertEqual(runtime.read_private(self.home / 'lifecycle.json')['status'], 'deleted')
        before = list(self.calls)
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, request), result)
        self.assertEqual(self.calls, before)
        self.edge_plan.assert_called_once()  # Apply validates the saved provider plan instead of minting another.
        with self.assertRaisesRegex(ValueError, 'APPLICATION_LIFECYCLE_BLOCKED'):
            self.fixture.register()

    def test_stop_then_start_restores_explicit_snapshot(self):
        _, stopped = self.apply(self.plan('stop'), 'stop')
        self.assertEqual(stopped['status'], 'succeeded', stopped)
        with self.assertRaisesRegex(ValueError, 'APPLICATION_LIFECYCLE_BLOCKED'):
            applications.assert_deployable(self.home)
        _, started = self.apply(self.plan('start'), 'start')
        self.assertEqual(started['status'], 'succeeded', started)
        self.assertEqual(self.execute.call_args.kwargs['stopped_inventory'], self.inventory)
        applications.assert_deployable(self.home)
        self.assertIn(self.app['application_id'], json.loads(self.fixture.fixture.variable['value']))

    def test_expired_or_drifted_plan_never_mutates(self):
        plan = self.plan(); self.calls.clear()
        self.inventory['namespace']['uid'] = 'replaced'
        with self.assertRaisesRegex(ValueError, 'LIFECYCLE_PLAN_CHANGED'):
            self.apply(plan)
        self.execute.assert_not_called(); self.edge_execute.assert_not_called()
        self.assertTrue(all(verb == 'get' for verb, _ in self.calls))
        self.inventory['namespace']['uid'] = 'namespace-1'
        path = self.home / 'lifecycle/plans' / (plan['plan_id'] + '.json')
        saved = runtime.read_private(path); saved['expires_at'] = datetime.fromtimestamp(0, timezone.utc).isoformat()
        runtime.save(path, saved); plan['plan_hash'] = applications.digest(saved)
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'LIFECYCLE_PLAN_EXPIRED'):
            self.apply(plan)
        self.assertEqual(self.calls, [])

    def test_provider_preflight_failure_has_zero_control_writes(self):
        plan = self.plan(); self.calls.clear()
        self.edge_validate.side_effect = ValueError('drift')
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.assertTrue(all(verb == 'get' for verb, _ in self.calls))
        self.execute.assert_not_called()
        self.assertFalse((self.home / 'lifecycle.json').exists())

    def test_partial_cleanup_is_unknown_and_cannot_replay(self):
        self.execute.side_effect = RuntimeError('private-secret-value')
        request, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['residuals'])
        self.assertNotIn('private-secret-value', json.dumps(result))
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, request), result)
        self.execute.assert_called_once()
        with self.assertRaisesRegex(ValueError, 'APPLICATION_LIFECYCLE_BLOCKED'):
            self.plan()

    def test_last_registration_removes_role_rules_instead_of_granting_all_secrets(self):
        cm = self.control.objects['argocd', 'configmap', 'railshot-credentials']
        policy = json.loads(cm['data']['policy.json'])
        policy['targets'] = [t for t in policy['targets'] if t['target_id'] == self.app['application_id']]
        cm['data']['policy.json'] = json.dumps(policy)
        role = self.control.objects['argocd', 'role', 'railshot-credentials']
        role['rules'][0]['resourceNames'] = [policy['targets'][0]['secret']]
        _, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.control.objects['argocd', 'role', 'railshot-credentials']['rules'], [])
        empty = {'version': 1, 'targets': []}
        runtime.credentials.validate_policy(empty)
        rendered = runtime.credentials.render(empty, 'ghcr.io/jasmin-softbank/railshot-api@sha256:' + 'a' * 64)
        self.assertEqual(next(item for item in rendered['items'] if item['kind'] == 'Role')['rules'], [])
        self.assertEqual(self.fixture.register('second-app')['status'], 'succeeded')

    def test_start_never_marks_ready_before_public_health(self):
        _, stopped = self.apply(self.plan('stop'), 'stop')
        self.assertEqual(stopped['status'], 'succeeded')
        plan = self.plan('start')
        with patch.object(runtime.bridge, 'public_probe', return_value={'state': 'unverified'}), \
                patch.object(lifecycle.time, 'monotonic', side_effect=[0, 121]):
            _, result = self.apply(plan, 'start')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['error']['code'], 'APPLICATION_PUBLIC_HEALTH_UNVERIFIED')
        self.assertNotIn(self.app['application_id'], json.loads(self.fixture.fixture.variable['value']))
        self.assertEqual(runtime.read_private(self.home / 'lifecycle.json')['status'], 'unknown')

    def test_unstarted_plan_cannot_delete_a_registration_that_appeared_later(self):
        self.app = self.fixture.request('queued-app'); self.home = self.fixture.home('queued-app')
        plan = self.plan()
        self.assertEqual(self.fixture.register('queued-app')['status'], 'succeeded')
        with self.assertRaisesRegex(ValueError, 'LIFECYCLE_PLAN_CHANGED'):
            self.apply(plan)
        self.execute.assert_not_called(); self.edge_execute.assert_not_called()

    def test_unstarted_registration_deletion_only_writes_local_tombstone(self):
        self.app = self.fixture.request('queued-app')
        self.home = self.fixture.home('queued-app')
        request, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.calls, [])
        self.edge_plan.assert_not_called(); self.edge_execute.assert_not_called(); self.execute.assert_not_called()
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, request), result)
        with self.assertRaisesRegex(ValueError, 'APPLICATION_LIFECYCLE_BLOCKED'):
            self.fixture.register('queued-app')

    def test_active_argo_sync_and_missing_confirmation_block(self):
        app = next(obj for (_, kind, _), obj in self.control.objects.items() if kind == 'application')
        app['operation'] = {'sync': {}}
        plan = self.plan()  # Deletion preview may precede cancellation/quiescence.
        with self.assertRaisesRegex(ValueError, 'APPLICATION_SYNC_ACTIVE'):
            self.apply(plan)
        del app['operation']
        plan = self.plan()
        request = self.request(phase='apply', plan_id=plan['plan_id'], plan_hash=plan['plan_hash'], delete_data=False)
        with self.assertRaisesRegex(ValueError, 'DELETE_DATA_CONFIRMATION_REQUIRED'):
            lifecycle.lifecycle(self.fixture.config_path, request)
        self.edge_execute.assert_not_called()
        self.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
