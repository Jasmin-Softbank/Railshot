"""Shared observer migration and app registration with native namespace RBAC."""
import base64
import copy
import json
import unittest
from unittest.mock import patch
import test_applications as fixtures
import environment as env


class SharedObserverTest(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ApplicationsTest(methodName='runTest'); self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        case = self.case; f = case.fixture
        old, new = case.env_id, 'k3s-aws'
        case.config['environments'][new] = case.config['environments'].pop(old)
        f.registry['targets'][new] = f.registry['targets'].pop(old)
        f.descriptor['target_id'] = new
        f.write('registry.json', f.registry); f.write('descriptor.json', f.descriptor)
        case.env_id = f.target = new; case.write_config()
        cm = f.control.objects['argocd', 'configmap', 'railshot-credentials']
        row = json.loads(cm['data']['policy.json'])['targets'][0]
        row.update(target_id=new, secret='railshot-'+new); cm['data']['policy.json'] = json.dumps({'version':1,'targets':[row]})
        secret = f.control.objects.pop(('argocd','secret','railshot-'+old))
        secret['metadata']['name'] = row['secret']; secret['data']['name'] = base64.b64encode(new.encode()).decode()
        f.control.objects['argocd','secret',row['secret']] = secret
        f.control.objects['argocd','role','railshot-credentials']['rules'][0]['resourceNames'] = [row['secret']]
        self.token_requests = 0
        def kube(namespace, *args, **kwargs):
            if args[:2] == ('get','namespaces'):
                return {'items':[copy.deepcopy(v) for (_, kind, _), v in f.runtime.objects.items() if kind == 'namespace']}
            result = f.runtime(namespace,*args,**kwargs)
            if args[:2] == ('create','--raw') and '/'+env.OBSERVER+'/' in args[2]:
                self.token_requests += 1
                token = result['status']['token']; parts = token.split('.'); payload=json.loads(base64.urlsafe_b64decode(parts[1]+'='*(-len(parts[1])%4)))
                payload['sub']='system:serviceaccount:'+namespace+':'+env.OBSERVER
                payload['kubernetes.io']['serviceaccount']['name']=env.OBSERVER
                parts[1]=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
                result['status']['token']='.'.join(parts)
            return result
        self.kube = kube
        old_customer = env.credentials.customer
        def customer(server, ca, token, path, document, **options):
            parts=token.split('.'); payload=json.loads(base64.urlsafe_b64decode(parts[1]+'='*(-len(parts[1])%4)))
            if payload['sub'].endswith(':'+env.OBSERVER):
                attr=document['spec']['resourceAttributes']
                return {'status':{'allowed':any(attr['resource'] in rule['resources'] and attr['group'] in rule['apiGroups'] and attr['verb'] in rule['verbs'] for rule in env.OBSERVER_RULES)}}
            return old_customer(server,ca,token,path,document,**options)
        self.enterContext(patch.object(env.credentials,'customer',customer))
        self.anchor = case.enable_fixed_cache()
        self.registered = {'target':{'cluster_server':self.anchor['server']}}

    def enable(self):
        return env.enable_shared_observer(self.kube, {'context':'control'}, self.registered, self.case.env_id)

    def test_existing_apps_are_bound_before_observer_is_published_and_new_apps_do_not_issue_tokens(self):
        legacy = self.case.register('legacy-app'); self.assertEqual(legacy['status'],'succeeded')
        row = self.enable(); self.assertEqual(self.token_requests,1)
        objects=self.case.fixture.control.objects; runtime=self.case.fixture.runtime.objects
        before=copy.deepcopy(objects['argocd','configmap','railshot-credentials']['data'])
        self.assertEqual(self.enable(),row); self.assertEqual(self.token_requests,1)
        binding=runtime[legacy['application_id'],'rolebinding','railshot-environment-observer']
        self.assertEqual(binding['subjects'],[{'kind':'ServiceAccount','name':env.OBSERVER,'namespace':'old-app'}])
        with patch.object(env,'register_argo',side_effect=AssertionError('no per-app token')), patch.object(env,'install_renewal',side_effect=AssertionError('no per-app renewal')):
            for app in ['first-app','second-app']:
                record=self.case.register(app); self.assertEqual(record['status'],'succeeded',record)
                app_id=record['application_id']
                self.assertNotIn(('argocd','secret','railshot-'+app_id),objects)
                self.assertNotIn((app_id,'serviceaccount',env.SA),runtime)
                self.assertNotIn((app_id,'rolebinding',env.SA),runtime)
                self.assertTrue(all(rule['resources'] != ['serviceaccounts/token'] for rule in runtime[app_id,'role',env.SA]['rules']))
                self.assertEqual(runtime[app_id,'role',env.OBSERVER]['rules'],env.OBSERVER_RULES)
                self.assertEqual(record['credentials']['renewal'],'environment')
        self.assertEqual(objects['argocd','configmap','railshot-credentials']['data'],before)
        self.assertEqual(row['namespaces'],['old-app'])
        self.assertEqual(objects['argocd','secret',row['secret']]['metadata']['labels']['argocd.argoproj.io/secret-type'],'railshot-observer')
        read=next(rule for rule in objects['argocd','role','railshot-product-registrations']['rules'] if rule['verbs']==['get'])
        self.assertEqual(read,{'apiGroups':[''],'resources':['secrets'],'verbs':['get'],'resourceNames':[row['secret']]})

    def test_invalid_observer_blocks_new_app_before_any_mutation_and_does_not_fall_back(self):
        row=self.enable(); secret=self.case.fixture.control.objects['argocd','secret',row['secret']]
        secret['metadata']['labels']['argocd.argoproj.io/secret-type']='cluster'
        before=(self.case.fixture.control.applications,self.case.fixture.runtime.applications)
        with self.assertRaises(ValueError):self.case.register()
        self.assertEqual(before,(self.case.fixture.control.applications,self.case.fixture.runtime.applications))

    def test_foreign_observer_binding_blocks_policy_publication(self):
        app=self.case.register(); app_id=app['application_id']
        self.case.fixture.runtime.objects[app_id,'rolebinding','railshot-environment-observer']={
            'metadata':{'name':'railshot-environment-observer','namespace':app_id,'labels':{'app.kubernetes.io/managed-by':'someone-else'}}}
        with self.assertRaisesRegex(ValueError,'another registration'):self.enable()
        policy=json.loads(self.case.fixture.control.objects['argocd','configmap','railshot-credentials']['data']['policy.json'])
        self.assertFalse(any(row['target_id'].startswith('observer-') for row in policy['targets']))

if __name__ == '__main__':unittest.main()
