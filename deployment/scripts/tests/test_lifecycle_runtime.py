"""Real lifecycle algorithm against a JSON Kubernetes fake; no native/cloud calls."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle_runtime as runtime

APP = 'app-' + 'a' * 24
LABELS = {'app.kubernetes.io/managed-by': 'railshot', 'railshot.io/registration': APP}
RESOURCE = {'Namespace': 'namespaces', 'ServiceAccount': 'serviceaccounts', 'Secret': 'secrets',
    'ConfigMap': 'configmaps', 'Pod': 'pods', 'Service': 'services', 'Endpoints': 'endpoints',
    'PersistentVolumeClaim': 'persistentvolumeclaims', 'PersistentVolume': 'persistentvolumes',
    'Deployment': 'deployments', 'StatefulSet': 'statefulsets', 'ReplicaSet': 'replicasets',
    'ControllerRevision': 'controllerrevisions', 'Job': 'jobs', 'CronJob': 'cronjobs',
    'NetworkPolicy': 'networkpolicies', 'Role': 'roles', 'RoleBinding': 'rolebindings', 'Event': 'events',
    'EndpointSlice': 'endpointslices', 'StorageClass': 'storageclasses', 'VolumeAttachment': 'volumeattachments',
    'Widget': 'widgets', 'CiliumEndpoint': 'ciliumendpoints'}
GV = {kind: group + '/v1' for group, kinds in runtime.SUPPORTED.items() if group for kind in kinds}
GV.update({'Event': 'v1', 'StorageClass': 'storage.k8s.io/v1', 'VolumeAttachment': 'storage.k8s.io/v1',
           'Widget': 'vendor.example/v1', 'CiliumEndpoint': 'cilium.io/v2'})
CLUSTER = {'Namespace', 'PersistentVolume', 'StorageClass', 'VolumeAttachment'}


def obj(kind, key, *, labels=None, owner=None, **body):
    result = {'apiVersion': GV.get(kind, 'v1'), 'kind': kind,
              'metadata': {'name': key, 'uid': kind.lower() + '-' + key.replace('.', '-'), 'resourceVersion': '1', 'generation': 1,
                           **({'namespace': APP} if kind not in CLUSTER else {}),
                           **({'labels': copy.deepcopy(LABELS if labels is None else labels)})}, **body}
    if owner:
        result['metadata']['ownerReferences'] = [{
            'apiVersion': owner['apiVersion'], 'kind': owner['kind'], 'name': owner['metadata']['name'],
            'uid': owner['metadata']['uid'], 'controller': True}]
    return result


def template():
    return {'metadata': {'labels': {'railshot.io/target': APP}}, 'spec': {'containers': [
        {'name': 'web', 'image': 'registry/web@sha256:' + 'b' * 64,
         'env': [{'name': 'PRIVATE_VALUE', 'value': 'secret-environment-never-persist'}]}],
         'volumes': [{'name': 'tmp', 'emptyDir': {}}]}}


class Kube:
    def __init__(self):
        deployment = obj('Deployment', 'web', labels={}, spec={'replicas': 3, 'template': template()})
        rs = obj('ReplicaSet', 'web-rs', labels={}, owner=deployment, spec={'template': template()})
        self.objects = [obj('Namespace', APP), obj('ServiceAccount', runtime.SA),
            obj('ServiceAccount', 'default', labels={}), obj('ConfigMap', 'kube-root-ca.crt', labels={}, data={'ca.crt': 'ca'}),
            obj('Secret', 'ghcr-pull', data={'.dockerconfigjson': 'secret-data-never-persist'}),
            obj('Role', runtime.SA, rules=[]), obj('RoleBinding', runtime.SA,
                roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': runtime.SA},
                subjects=[{'kind': 'ServiceAccount', 'name': runtime.SA, 'namespace': APP}]),
            deployment, rs, obj('Pod', 'web-pod', labels={}, owner=rs, spec=template()['spec'], status={'phase': 'Running'}),
            obj('Service', 'web', labels={}, spec={'selector': {'railshot.io/target': APP}, 'type': 'NodePort', 'ports': [{'nodePort': 31000}]}),
            obj('NetworkPolicy', 'web', labels={}, spec={'podSelector': {'matchLabels': {'railshot.io/target': APP}}})]
        self.writes = []; self.reads = []; self.quiesce = True; self.clean_storage = True
        self.write_error = False; self.before_write = None; self.workloads_ready = True
        self.discovery = set(RESOURCE) - {'Widget'}
        self.item_type_meta = False

    def find(self, kind, key):
        return next((item for item in self.objects if item['kind'] == kind and item['metadata']['name'] == key), None)

    def __call__(self, ns, *args, document=None):
        assert ns == APP
        if args[0] in ('patch', 'delete'):
            self.writes.append((args, copy.deepcopy(document)))
            if self.before_write:
                self.before_write()
            if self.write_error:
                raise RuntimeError('sensitive raw provider stderr must not surface')
            if args[0] == 'patch':
                kind = next(k for k, v in RESOURCE.items() if args[1].split('.')[0] == v)
                live = self.find(kind, args[2])
                assert document[0]['value'] == live['metadata']['uid']
                assert document[1]['value'] == live['metadata']['resourceVersion']
                field = document[2]['path'].split('/')[-1]
                live['spec'][field] = document[2]['value']
                live['metadata']['resourceVersion'] = str(int(live['metadata']['resourceVersion']) + 1)
                live['metadata']['generation'] += 1
                if field == 'replicas' and self.workloads_ready:
                    live['status'] = {'observedGeneration': live['metadata']['generation'], 'readyReplicas': document[2]['value']}
                if self.quiesce:
                    self.objects = [o for o in self.objects if o['kind'] not in ('Pod', 'CiliumEndpoint') or o.get('status', {}).get('phase') in ('Succeeded', 'Failed')]
                return copy.deepcopy(live)
            assert args == ('delete', '--raw', '/api/v1/namespaces/' + APP, '-f', '-')
            namespace = self.find('Namespace', APP)
            assert document['preconditions']['uid'] == namespace['metadata']['uid']
            assert document['preconditions']['resourceVersion'] == namespace['metadata']['resourceVersion']
            self.objects = [o for o in self.objects if o['kind'] in CLUSTER and o['kind'] != 'Namespace' and
                            not (self.clean_storage and o['kind'] in ('PersistentVolume', 'VolumeAttachment'))]
            return {'kind': 'Status', 'status': 'Success'}
        self.reads.append(args)
        if args[:2] == ('get', '--raw'):
            path = urlsplit(args[2]).path
            if path == '/api':
                return {'versions': ['v1']}
            if path == '/apis':
                versions = sorted({GV.get(kind, 'v1') for kind in self.discovery} - {'v1'})
                return {'groups': [{'preferredVersion': {'groupVersion': v}, 'versions': [{'groupVersion': v}]} for v in versions]}
            prefix = '/apis/' if path.startswith('/apis/') else '/api/'
            tail = path[len(prefix):].split('/')
            length = 2 if prefix == '/apis/' else 1
            gv = '/'.join(tail[:length])
            if len(tail) == length:
                return {'resources': [{'name': RESOURCE[kind], 'kind': kind, 'namespaced': kind not in CLUSTER,
                    'verbs': ['get', 'list']} for kind in sorted(self.discovery) if GV.get(kind, 'v1') == gv]}
            resource = tail[-1]
            items = [o for o in self.objects if o['apiVersion'] == gv and RESOURCE[o['kind']] == resource]
            if 'namespaces' in tail:
                items = [o for o in items if o['metadata'].get('namespace') == APP]
            kind = next(k for k, value in RESOURCE.items() if value == resource)
            items = copy.deepcopy(items)
            if not self.item_type_meta:
                for item in items:
                    item.pop('kind'); item.pop('apiVersion')
            return {'apiVersion': gv, 'kind': kind + 'List', 'metadata': {}, 'items': items}
        assert args[0] == 'get' and args[-3:] == ('--ignore-not-found', '-o', 'json'), args
        kind = next(k for k, resource in RESOURCE.items() if args[1].split('.')[0].lower() in (resource, k.lower()))
        return copy.deepcopy(self.find(kind, args[2]))


class LifecycleRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.kube = Kube()
        self.binding = {'version': 1, 'application_id': APP, 'environment_id': 'environment-aws',
                        'provider': 'aws', 'registered': {'app': 'web', 'target': {'id': APP, 'namespace': APP}}}
        self.renewal = {'service_account': {'name': runtime.SA, 'namespace': APP,
                                          'uid': self.kube.find('ServiceAccount', runtime.SA)['metadata']['uid']}}

    def inventory(self, action='stop'):
        return runtime.inventory(self.kube, self.binding, self.renewal, action)

    def blocked(self, code, function):
        with self.assertRaises(runtime.LifecycleRuntimeError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def storage(self):
        claim = obj('PersistentVolumeClaim', 'data', spec={'volumeName': 'pv-data', 'storageClassName': 'csi-fast'}, status={'phase': 'Bound'})
        volume = obj('PersistentVolume', 'pv-data', spec={'csi': {'driver': 'csi.test', 'volumeHandle': 'private-volume-handle',
            'volumeAttributes': {'storage.kubernetes.io/csiProvisionerIdentity': 'dynamic-controller'}},
            'persistentVolumeReclaimPolicy': 'Delete', 'storageClassName': 'csi-fast', 'accessModes': ['ReadWriteOnce'],
            'claimRef': {'uid': claim['metadata']['uid'], 'name': 'data', 'namespace': APP}})
        volume['metadata'].update(finalizers=[runtime.FINALIZER], annotations={'pv.kubernetes.io/provisioned-by': 'csi.test'})
        self.kube.objects.extend([claim, volume, obj('StorageClass', 'csi-fast', provisioner='csi.test', reclaimPolicy='Delete'),
            obj('VolumeAttachment', 'attachment-data', spec={'source': {'persistentVolumeName': 'pv-data'}})])
        return claim, volume

    def test_preview_is_read_only_secret_free_and_accepts_current_unlabelled_workload_roots(self):
        snapshot = self.inventory('delete')
        encoded = json.dumps(snapshot)
        self.assertNotIn('secret-environment-never-persist', encoded)
        self.assertNotIn('secret-data-never-persist', encoded)
        self.assertEqual(self.kube.writes, [])
        self.assertTrue(any(r['kind'] == 'Namespace' for r in snapshot['resources']))
        self.assertTrue(all(set(r) <= {'kind', 'name', 'namespace'} for r in snapshot['resources'] + snapshot['retained']))
        self.assertEqual(snapshot['service_account'], self.renewal['service_account'])

    def test_uid_namespace_label_and_foreign_child_ownership_fail_before_writes(self):
        for change, code in [
            (lambda: self.renewal['service_account'].update(uid='replacement'), 'APPLICATION_RUNTIME_SERVICE_ACCOUNT_MISMATCH'),
            (lambda: self.kube.find('Namespace', APP)['metadata']['labels'].update({runtime.LABEL: 'other'}), 'APPLICATION_RUNTIME_NAMESPACE_MISMATCH'),
            (lambda: self.kube.find('Pod', 'web-pod')['metadata']['ownerReferences'][0].update(uid='foreign'), 'APPLICATION_RUNTIME_FOREIGN_OWNER')]:
            with self.subTest(code=code):
                self.setUp(); change(); self.blocked(code, self.inventory); self.assertFalse(self.kube.writes)

    def test_full_discovery_rejects_namespaced_custom_resource_even_without_app_label(self):
        self.kube.discovery.add('Widget'); self.kube.objects.append(obj('Widget', 'managed', labels={}))
        self.blocked('APPLICATION_RUNTIME_UNSUPPORTED_RESOURCE', self.inventory)
        self.assertFalse(self.kube.writes)

    def test_empty_custom_resource_api_is_not_a_blocker(self):
        self.kube.discovery.add('Widget')
        self.inventory()

    def test_only_exact_pod_owned_cilium_endpoint_is_supported_and_ephemeral(self):
        original = self.inventory()
        endpoint = obj('CiliumEndpoint', 'web-pod', labels={}, owner=self.kube.find('Pod', 'web-pod'))
        self.kube.objects.append(endpoint)
        self.assertEqual(runtime.comparable(original), runtime.comparable(self.inventory()))
        endpoint['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        self.blocked('APPLICATION_RUNTIME_FOREIGN_OWNER', self.inventory)
        endpoint['metadata'].pop('ownerReferences')
        self.blocked('APPLICATION_RUNTIME_FOREIGN_OWNER', self.inventory)

    def test_event_aliases_and_history_do_not_change_approved_resources(self):
        preview = self.inventory()
        event = obj('Event', 'history', labels={}, involvedObject={'kind': 'Pod', 'uid': 'old-deleted-pod'})
        self.kube.objects.extend([event, copy.deepcopy(event)])
        self.assertEqual(runtime.comparable(preview), runtime.comparable(self.inventory()))

    def test_paginated_discovery_does_not_omit_resources_after_first_page(self):
        native = self.kube
        def paginated(ns, *args, **kwargs):
            result = native(ns, *args, **kwargs)
            if args[:2] == ('get', '--raw') and '/secrets?' in args[2]:
                if '&continue=' not in args[2]:
                    return {'metadata': {'continue': 'next/page'}, 'items': []}
                self.assertIn('&continue=next%2Fpage', args[2])
            return result
        snapshot = runtime.inventory(paginated, self.binding, self.renewal, 'delete')
        self.assertTrue(any(r['kind'] == 'Secret' for r in snapshot['records']))

    def test_discovery_read_failure_is_typed_and_sanitized(self):
        def broken(*args, **kwargs):
            raise ValueError('private connection credential')
        error = self.blocked('APPLICATION_RUNTIME_READ_FAILED', lambda: runtime.inventory(broken, self.binding, self.renewal, 'delete'))
        self.assertNotIn('credential', str(error)); self.assertFalse(error.unknown)

    def test_declaration_drift_and_recreated_uid_reject_approved_plan(self):
        for change in (lambda: self.kube.find('Deployment', 'web')['spec']['template']['spec']['containers'][0].update(image='unapproved'),
                       lambda: self.kube.find('Service', 'web')['metadata'].update(uid='replacement'),
                       lambda: self.kube.find('Secret', 'ghcr-pull')['data'].update(token='changed')):
            with self.subTest(change=change):
                self.setUp(); preview = self.inventory(); change()
                self.blocked('APPLICATION_RUNTIME_PLAN_STALE', lambda: runtime.execute(self.kube, self.binding, 'stop', preview))
                self.assertFalse(self.kube.writes)

    def test_pod_status_resource_version_and_new_owned_pod_do_not_stale_plan(self):
        preview = self.inventory()
        pod = self.kube.find('Pod', 'web-pod'); pod['status']['phase'] = 'Pending'; pod['metadata']['resourceVersion'] = '9'
        self.kube.objects.append(obj('Pod', 'new-pod', labels={}, owner=self.kube.find('ReplicaSet', 'web-rs'), status={'phase': 'Running'}))
        self.kube.find('Deployment', 'web')['status'] = {'readyReplicas': 2}
        self.kube.find('Deployment', 'web')['metadata']['annotations'] = {'deployment.kubernetes.io/revision': '2'}
        result = runtime.execute(self.kube, self.binding, 'stop', preview)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.kube.find('Deployment', 'web')['spec']['replicas'], 0)

    def test_stop_and_start_restore_exact_replicas_and_prior_suspend_controls(self):
        cron = obj('CronJob', 'schedule', spec={'schedule': '0 * * * *', 'suspend': True,
                                             'jobTemplate': {'spec': {'template': template()}}})
        job = obj('Job', 'job', spec={'template': template(), 'suspend': False})
        self.kube.objects.extend([cron, job, obj('Pod', 'completed', labels={}, owner=job, status={'phase': 'Succeeded'})])
        original = self.inventory()
        stopped = runtime.execute(self.kube, self.binding, 'stop', original)
        self.assertIn({'name': 'no-running-pods', 'status': 'succeeded'}, stopped['steps'])
        self.assertIsNotNone(self.kube.find('Pod', 'completed'))
        self.assertTrue(job['spec']['suspend'])
        preview = self.inventory('start')
        runtime.execute(self.kube, self.binding, 'start', preview, stopped_inventory=original)
        self.assertEqual(self.kube.find('Deployment', 'web')['spec']['replicas'], 3)
        self.assertTrue(cron['spec']['suspend']); self.assertFalse(job['spec']['suspend'])
        self.assertTrue(all(command[1] != 'apply' for command, _ in self.kube.writes))

    def test_start_requires_original_snapshot(self):
        preview = self.inventory('start')
        self.blocked('APPLICATION_RUNTIME_STOP_SNAPSHOT_MISMATCH', lambda: runtime.execute(self.kube, self.binding, 'start', preview))
        self.assertFalse(self.kube.writes)

    def test_start_requires_current_generation_ready_replicas(self):
        original = self.inventory()
        runtime.execute(self.kube, self.binding, 'stop', original)
        current = self.inventory('start'); self.kube.workloads_ready = False
        with patch.object(runtime, 'TIMEOUT', 0):
            error = self.blocked('APPLICATION_RUNTIME_TIMEOUT', lambda: runtime.execute(
                self.kube, self.binding, 'start', current, stopped_inventory=original))
        self.assertTrue(error.unknown)
        self.assertEqual(self.kube.find('Deployment', 'web')['spec']['replicas'], 3)

    def test_stateful_set_restores_exact_replicas_and_readiness(self):
        stateful = obj('StatefulSet', 'stateful', spec={'replicas': 2, 'template': template()})
        self.kube.objects.append(stateful)
        original = self.inventory()
        runtime.execute(self.kube, self.binding, 'stop', original)
        self.assertEqual(stateful['spec']['replicas'], 0)
        result = runtime.execute(self.kube, self.binding, 'start', self.inventory('start'), stopped_inventory=original)
        self.assertEqual(stateful['spec']['replicas'], 2)
        self.assertIn({'name': 'workloads-ready', 'status': 'succeeded'}, result['steps'])

    def test_mutation_uncertainty_sends_once_and_reports_possible_residuals(self):
        preview = self.inventory(); self.kube.write_error = True
        error = self.blocked('APPLICATION_RUNTIME_WRITE_UNCERTAIN', lambda: runtime.execute(self.kube, self.binding, 'stop', preview))
        self.assertTrue(error.unknown); self.assertEqual(len(self.kube.writes), 1)
        self.assertTrue(error.residuals); self.assertNotIn('provider', str(error))

    def test_partial_stop_preserves_completed_steps_and_does_not_retry_failed_write(self):
        self.kube.objects.append(obj('StatefulSet', 'stateful', spec={'replicas': 2, 'template': template()}))
        preview = self.inventory()
        def fail_second():
            if len(self.kube.writes) == 2:
                raise RuntimeError('uncertain second write')
        self.kube.before_write = fail_second
        error = self.blocked('APPLICATION_RUNTIME_WRITE_UNCERTAIN', lambda: runtime.execute(self.kube, self.binding, 'stop', preview))
        self.assertTrue(error.unknown); self.assertEqual(len(self.kube.writes), 2)
        self.assertEqual(len(error.steps), 1)
        self.assertEqual(self.kube.find('Deployment', 'web')['spec']['replicas'], 0)
        self.assertEqual(self.kube.find('StatefulSet', 'stateful')['spec']['replicas'], 2)

    def test_uid_race_is_guarded_by_json_patch_test(self):
        preview = self.inventory()
        self.kube.before_write = lambda: self.kube.find('Deployment', 'web')['metadata'].update(uid='replacement')
        error = self.blocked('APPLICATION_RUNTIME_WRITE_UNCERTAIN', lambda: runtime.execute(self.kube, self.binding, 'stop', preview))
        self.assertTrue(error.unknown); self.assertEqual(self.kube.find('Deployment', 'web')['spec']['replicas'], 3)

    def test_stop_timeout_is_unknown_and_completed_steps_are_preserved(self):
        preview = self.inventory(); self.kube.quiesce = False
        with patch.object(runtime, 'TIMEOUT', 0):
            error = self.blocked('APPLICATION_RUNTIME_TIMEOUT', lambda: runtime.execute(self.kube, self.binding, 'stop', preview))
        self.assertTrue(error.unknown); self.assertTrue(error.steps); self.assertTrue(error.residuals)

    def test_delete_uses_namespace_uid_and_resource_version_preconditions_without_force(self):
        preview = self.inventory('delete')
        result = runtime.execute(self.kube, self.binding, 'delete', preview)
        self.assertEqual(result['status'], 'succeeded'); self.assertEqual(result['residuals'], [])
        command, body = self.kube.writes[0]
        self.assertEqual(body['preconditions']['uid'], preview['namespace']['uid'])
        self.assertEqual(body['preconditions']['resourceVersion'], '1')
        self.assertEqual(body['kind'], 'DeleteOptions')
        self.assertNotIn('--force', command); self.assertNotIn('finalizers', json.dumps(body))

    def test_csi_dynamic_delete_waits_for_pv_and_attachment_backing_contract(self):
        self.storage(); preview = self.inventory('delete')
        self.assertNotIn('private-volume-handle', json.dumps(preview))
        self.assertEqual(runtime.execute(self.kube, self.binding, 'delete', preview)['status'], 'succeeded')
        self.assertIsNone(self.kube.find('PersistentVolume', 'pv-data'))
        self.assertIsNone(self.kube.find('VolumeAttachment', 'attachment-data'))

    def test_native_typed_lists_without_item_type_meta_preserve_discovery_and_storage_inventory(self):
        self.storage()
        raw = self.kube(APP, 'get', '--raw', '/api/v1/persistentvolumes?limit=500')
        self.assertEqual((raw['kind'], raw['apiVersion']), ('PersistentVolumeList', 'v1'))
        self.assertNotIn('kind', raw['items'][0]); self.assertNotIn('apiVersion', raw['items'][0])
        preview = self.inventory('delete')
        self.assertEqual(preview['storage'][0]['volume'], {'kind': 'PersistentVolume', 'name': 'pv-data'})
        self.assertEqual(preview['storage'][0]['attachments'], [{'kind': 'VolumeAttachment', 'name': 'attachment-data'}])
        self.assertTrue(any(row['kind'] == 'Deployment' and row['apiVersion'] == 'apps/v1' for row in preview['records']))
        self.kube.item_type_meta = True
        self.assertEqual(self.inventory('delete'), preview)
        self.kube.item_type_meta = False
        self.assertEqual(runtime.execute(self.kube, self.binding, 'delete', preview)['status'], 'succeeded')

    def test_declared_list_or_item_type_conflicts_fail_closed_for_discovery_and_storage(self):
        self.storage()
        for endpoint in ('/deployments?', '/persistentvolumes?', '/volumeattachments?'):
            for location, field, value in (('list', 'kind', 'SecretList'), ('list', 'apiVersion', 'foreign.example/v1'),
                                           ('item', 'kind', 'Secret'), ('item', 'apiVersion', 'foreign.example/v1'),
                                           ('item', 'kind', None), ('item', 'apiVersion', None)):
                with self.subTest(endpoint=endpoint, location=location, field=field, value=value):
                    def invalid(ns, *args, **kwargs):
                        result = self.kube(ns, *args, **kwargs)
                        if args[:2] == ('get', '--raw') and endpoint in args[2]:
                            target = result if location == 'list' else result['items'][0]
                            target[field] = value
                        return result
                    self.blocked('APPLICATION_RUNTIME_DISCOVERY_FAILED', lambda: runtime.inventory(
                        invalid, self.binding, self.renewal, 'delete'))
        self.assertFalse(self.kube.writes)

    def test_each_native_list_page_validates_its_type_before_using_items(self):
        self.storage()
        def wrong_second_page(ns, *args, **kwargs):
            result = self.kube(ns, *args, **kwargs)
            if args[:2] == ('get', '--raw') and '/persistentvolumes?' in args[2]:
                if '&continue=' not in args[2]:
                    result['metadata']['continue'] = 'page-two'
                    result['items'] = []
                else:
                    result['kind'] = 'SecretList'
            return result
        self.blocked('APPLICATION_RUNTIME_DISCOVERY_FAILED', lambda: runtime.inventory(
            wrong_second_page, self.binding, self.renewal, 'delete'))
        self.assertFalse(self.kube.writes)

    def test_storage_or_attachment_remaining_is_not_success(self):
        self.storage(); preview = self.inventory('delete'); self.kube.clean_storage = False
        with patch.object(runtime, 'TIMEOUT', 0):
            error = self.blocked('APPLICATION_RUNTIME_TIMEOUT', lambda: runtime.execute(self.kube, self.binding, 'delete', preview))
        self.assertTrue(error.unknown); self.assertTrue(any(r['kind'] == 'PersistentVolume' for r in error.residuals))

    def test_retain_static_local_path_and_shared_storage_fail_closed(self):
        for mutation in ('retain', 'static', 'local', 'shared', 'no-finalizer'):
            with self.subTest(mutation=mutation):
                self.setUp(); _, volume = self.storage()
                code = 'APPLICATION_RUNTIME_UNSUPPORTED_STORAGE'
                if mutation == 'retain': volume['spec']['persistentVolumeReclaimPolicy'] = 'Retain'
                if mutation == 'static': volume['spec']['csi']['volumeAttributes'] = {}
                if mutation == 'local': volume['spec'].pop('csi'); volume['spec']['hostPath'] = {'path': '/data'}
                if mutation == 'no-finalizer': volume['metadata']['finalizers'] = []
                if mutation == 'shared':
                    duplicate = copy.deepcopy(volume); duplicate['metadata'].update(name='other-pv', uid='other-pv')
                    self.kube.objects.append(duplicate); code = 'APPLICATION_RUNTIME_SHARED_STORAGE'
                self.blocked(code, lambda: self.inventory('delete')); self.assertFalse(self.kube.writes)

    def test_external_database_and_host_path_and_external_service_rejected(self):
        self.binding['registered']['target']['database'] = {'host': 'private-db'}
        self.blocked('APPLICATION_RUNTIME_EXTERNAL_DATABASE', lambda: self.inventory('delete'))
        self.binding['registered']['target'].pop('database')
        self.kube.find('Deployment', 'web')['spec']['template']['spec']['volumes'] = [{'name': 'data', 'hostPath': {'path': '/data'}}]
        self.blocked('APPLICATION_RUNTIME_EXTERNAL_STORAGE', self.inventory)
        self.setUp(); self.kube.find('Service', 'web')['spec']['type'] = 'LoadBalancer'
        self.blocked('APPLICATION_RUNTIME_UNSUPPORTED_SERVICE', self.inventory)

    def test_new_namespace_with_same_name_is_not_declared_deleted(self):
        snapshot = self.inventory('delete')
        self.kube.find('Namespace', APP)['metadata']['uid'] = 'replacement'
        self.blocked('APPLICATION_RUNTIME_NAMESPACE_REPLACED', lambda: runtime.deleted(self.kube, APP, snapshot))

    def test_foreign_role_binding_cannot_be_adopted_by_label_alone(self):
        self.kube.find('RoleBinding', runtime.SA)['subjects'][0]['namespace'] = 'other'
        self.blocked('APPLICATION_RUNTIME_FOREIGN_OWNER', self.inventory)


if __name__ == '__main__':
    unittest.main()
