import copy
import json
import unittest
from unittest.mock import patch
import workload_diagnostics
from test_logs import LogsTest

class WorkloadDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.f = LogsTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.deployment['status'] = {'observedGeneration':1,'availableReplicas':1}
        c = self.f.pod['status']['containerStatuses'][0]
        c.update(ready=True,restartCount=0,state={'running':{}})
        self.f.pod['status']['conditions']=[{'type':'Ready','status':'True'}]

    def observe(self):
        with patch('argo.kubectl',side_effect=self.f.control), patch('credentials.customer',side_effect=self.f.customer):
            return workload_diagnostics.workload(self.f.config,self.f.review)

    def test_readiness_requires_exact_pod_owner_digest_and_observed_generation(self):
        self.assertEqual(self.observe()['state'],'ready')
        self.f.deployment['spec']['strategy'] = {'type': 'RollingUpdate', 'rollingUpdate': {'maxUnavailable': 0, 'maxSurge': 1}}
        self.f.deployment['status']['updatedReplicas'] = 1
        observed = self.observe()
        self.assertEqual(observed['update_strategy'], {'type': 'RollingUpdate', 'max_unavailable': 0, 'max_surge': 1})
        self.assertEqual(observed['replicas'], {'updated': 1, 'available': 1, 'ready': None})
        for change in ('owner','image','generation','error'):
            before=copy.deepcopy((self.f.pod,self.f.deployment))
            if change=='owner': self.f.pod['metadata']['ownerReferences'][0]['uid']='foreign'
            if change=='image': self.f.pod['status']['containerStatuses'][0]['imageID']='containerd://sha256:'+'f'*64
            if change=='generation': self.f.deployment['status']['observedGeneration']=0
            if change=='error': self.f.pod['status']['containerStatuses'][0].update(ready=False,state={'terminated':{'reason':'OOMKilled','exitCode':137,'message':'private-secret'}})
            result=self.observe()
            self.assertEqual(result['state'],'progressing')
            self.assertNotIn('private-secret',json.dumps(result))
            if change=='error': self.assertEqual(result['pods'][0]['containers'][0]['reason'],'OOMKilled')
            self.f.pod,self.f.deployment=before

    def test_drained_counts_only_live_pods_of_this_deployments_older_or_terminating_replicasets(self):
        observed = self.observe()
        self.assertEqual((observed['state'], observed['drained'], observed['obsolete_pods']), ('ready', True, 0))
        old_rs = copy.deepcopy(self.f.rs)
        old_rs['metadata'].update(name='demo-old', uid='old-rs-uid')
        old_rs['spec']['template']['spec']['containers'][0]['image'] = 'registry.example/old@sha256:' + 'a' * 64
        foreign_rs = copy.deepcopy(old_rs)
        foreign_rs['metadata'].update(name='demo-foreign', uid='foreign-rs-uid',
                                      ownerReferences=[{'kind': 'Deployment', 'uid': 'other-deployment', 'controller': True}])
        def pod(name, owner, **changes):
            value = copy.deepcopy(self.f.pod)
            value['metadata'].update(name=name, uid=name, ownerReferences=[{'kind': 'ReplicaSet', 'uid': owner, 'controller': True}])
            for key, item in changes.items():
                (value['metadata'] if key == 'deletionTimestamp' else value['status'])[key] = item
            return value
        cases = {
            'old pod still serving': ([pod('old', 'old-rs-uid')], False),
            'old pod terminating': ([pod('old', 'old-rs-uid', deletionTimestamp='2026-10-06T04:55:05Z')], False),
            'surplus current pod terminating': ([pod('extra', 'rs-uid', deletionTimestamp='2026-10-06T04:55:05Z')], False),
            'same labels, other controller': ([pod('foreign', 'foreign-rs-uid')], True),
            'old pod already terminal': ([pod('old', 'old-rs-uid', phase='Failed')], True),
            'fully drained': ([], True)}
        customer = self.f.customer
        for name, (extra, drained) in cases.items():
            with self.subTest(name=name):
                def read(*args, **kwargs):
                    value = customer(*args, **kwargs)
                    if '/replicasets?' in args[3]: value = {**value, 'items': [self.f.rs, old_rs, foreign_rs]}
                    if '/pods?' in args[3]: value = {**value, 'items': [self.f.pod, *extra]}
                    return value
                with patch('argo.kubectl', side_effect=self.f.control), patch('credentials.customer', side_effect=read):
                    result = workload_diagnostics.workload(self.f.config, self.f.review)
                # The requested image remains ready; only draining decides completion.
                self.assertEqual((result['state'], result['drained'], result['obsolete_pods']), ('ready', drained, 0 if drained else 1))
                self.assertEqual([row['name'] for row in result['pods']], ['demo-rs-pod'])

    def test_transport_failure_is_unavailable_and_never_fabricates_readiness(self):
        with patch('logs.customer_auth',side_effect=ValueError('private-secret')):
            result=workload_diagnostics.workload(self.f.config,self.f.review)
        self.assertEqual(result['state'],'unavailable'); self.assertNotIn('private-secret',json.dumps(result))
        self.assertNotIn('replicas', result)

    def test_typed_deployment_list_items_may_omit_kind_but_cannot_change_identity(self):
        self.f.deployment.pop('kind')
        self.f.deployment.pop('apiVersion', None)
        self.assertEqual(self.observe()['state'], 'ready')
        for kind in ('Pod', '', None):
            self.f.deployment['kind'] = kind
            self.assertEqual(self.observe()['state'], 'unavailable')
        self.f.deployment.pop('kind')
        self.f.deployment['metadata']['namespace'] = 'foreign'
        self.assertEqual(self.observe()['state'], 'unavailable')

    def test_authorized_empty_deployment_list_is_missing_not_transport_failure(self):
        with patch('logs.customer_auth', return_value=(('server', b'CA', 'token'), {})), \
                patch('credentials.customer', return_value={'kind':'DeploymentList','items':[]}) as read:
            result=workload_diagnostics.workload(self.f.config,self.f.review)
        self.assertEqual(result['state'],'missing'); self.assertEqual(result['code'],'WORKLOAD_MISSING')
        self.assertIn('fieldSelector=metadata.name%3D',read.call_args.args[3])
        self.assertEqual(read.call_count,1)

    def test_fresh_observation_reports_missing_workload_even_after_past_success(self):
        with patch('logs.load_bound_review', return_value=self.f.review), \
                patch('workload_diagnostics.workload', return_value={'state':'missing','checked_at':'2026-10-05T00:00:00Z','pods':[],'code':'WORKLOAD_MISSING'}), \
                patch('bridge.public_probe', return_value={'state':'unverified','verified_at':None,'url':None}):
            self.f.config['targets']['k3s-aws']['public_http']={'url':'https://app.example/health','expected_status':200}
            result=workload_diagnostics.observe(self.f.config,self.f.value)
        self.assertEqual(result['state'],'ready'); self.assertEqual(result['reason'],'WORKLOAD_MISSING')
        self.assertEqual(result['public_http']['state'],'unverified')

if __name__=='__main__': unittest.main()
