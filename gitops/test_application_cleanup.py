import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import application_cleanup as cleanup
import dns
import gcp_routes
from test_gcp_routes import app, plan_for


def state_resources(rows):
    resources = []
    for address, attributes in rows.items():
        kind, rest = address.split('.', 1)
        name, _, index = rest.partition('[')
        item = {'attributes': attributes}
        if index:
            item['index_key'] = json.loads(index[:-1])
        resources.append({'mode': 'managed', 'type': kind, 'name': name, 'instances': [item]})
    return resources


def state_rows(state):
    rows = {}
    for resource in state['resources']:
        for item in resource['instances']:
            address = resource['type'] + '.' + resource['name']
            if 'index_key' in item:
                address += '[' + json.dumps(item['index_key']) + ']'
            rows[address] = item['attributes']
    return rows


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.request = app()
        self.base = {'project_id': 'fixture-project', 'zone': 'test-a', 'instance_name': 'existing-vm',
                     'expected_private_ip': '10.1.0.2', 'hostname': 'legacy.example.com', 'routes': {}}
        self.creation = plan_for(self.request, self.base)
        neg_name = self.creation['resource_changes'][0]['change']['after']['name']
        self.creation['resource_changes'][1]['change']['after']['network_endpoint_group'] = neg_name
        rows = {'google_compute_global_address.app': {'id': 'shared-public-ip', 'address': '34.1.2.3'}}
        for change in self.creation['resource_changes']:
            values = copy.deepcopy(change['change']['after'])
            values['id'] = values.get('id') or change['address'] + '-original-id'
            values.pop('fingerprint', None)
            rows[change['address']] = values
        self.state = {'version': 4, 'lineage': 'original-lineage', 'serial': 10, 'resources': state_resources(rows)}
        self.values = {**self.base, 'routes': {self.request['application_id']: gcp_routes.checked_request(self.request)}}
        self.config = {'version': 1, 'provider': 'gcp', 'edge_kind': 'native', 'state_file': str(self.root / 'state.json'),
                       'variables_file': str(self.root / 'vars.json'), 'state_dir': str(self.root / 'edge'),
                       'state_lineage': self.state['lineage'], 'owned_resources': gcp_routes.owned(self.state),
                       'previous_source_sha': 'a' * 40}
        self.config_path = self.root / 'config.json'
        self.write(self.config_path, self.config); self.write(self.config['state_file'], self.state)
        self.write(self.config['variables_file'], self.values)
        self.dns_config = {'version': 1, 'zone_id': 'a' * 32, 'base_domain': 'railshot.io',
                           'token_file': str(self.root / 'token'), 'state_dir': str(self.root / 'dns')}
        self.write(self.root / 'dns.json', self.dns_config)
        self.binding = {'version': 1, 'environment_id': 'environment-one', 'application_id': self.request['application_id'],
                        'provider': 'gcp', 'registered': {'tenant': 'team', 'app': 'user-app', 'target': {
                            'id': self.request['application_id'], 'namespace': self.request['application_id'],
                            'node_port': self.request['node_port']}}, 'hostname': self.request['hostname'],
                        'ingress': {'edge_config_file': str(self.config_path), 'dns_config_file': str(self.root / 'dns.json')}}
        self.calls, self.documents = [], {}
        self.fail_apply = False
        self.mutate_plan = None
        self.mutations, self.applies = {}, []
        self.after_drift = False
        self.applied = False
        self.native_patch = patch('edge.native', side_effect=self.native)
        self.native_patch.start(); self.addCleanup(self.native_patch.stop)
        self.dns_patch = patch('dns.records_at', return_value=[])
        self.dns_patch.start(); self.addCleanup(self.dns_patch.stop)

    def write(self, path, value):
        cleanup.durable_write(path, cleanup.encoded(value))

    def document(self, candidate):
        current = cleanup.read_private(self.config['variables_file'])
        state = cleanup.read_private(self.config['state_file'])
        rows = state_rows(state)
        old = current['routes'].get(self.request['application_id'])
        new = candidate['routes'].get(self.request['application_id'])
        was = any(address.startswith('google_compute_backend_service.routes[') for address in rows)
        now = new is not None and new.get('enabled', True)
        # URL maps follow routed routes; the backend, NEG and firewall port follow enabled routes.
        routed_now = now and self.request['application_id'] not in candidate.get('detached_routes', [])
        changes = []
        for source in self.creation['resource_changes']:
            address = source['address']; kind = address.split('.')[0]
            if source['change']['actions'] == ['create']:
                should = new is not None if kind in gcp_routes.KINDS[4:] else now
                exists = address in rows
                if exists and not should:
                    change = {'actions': ['delete'], 'before': rows[address], 'after': None}
                elif should and not exists:
                    change = copy.deepcopy(source['change'])
                elif exists:
                    change = {'actions': ['no-op'], 'before': rows[address], 'after': rows[address]}
                else:
                    continue
            else:
                live, wanted = (self.routes(rows[address]), routed_now) if address in cleanup.DETACH else (was, now)
                if live == wanted:
                    change = {'actions': ['no-op'], 'before': rows[address], 'after': rows[address]}
                else:
                    after = copy.deepcopy(source['change']['after' if wanted else 'before'])
                    after.pop('fingerprint', None)
                    after['id'] = rows[address]['id']
                    change = {'actions': ['update'], 'before': rows[address], 'after': after}
            changes.append({'address': address, 'change': change})
        address = 'google_compute_global_address.app'
        changes.append({'address': address, 'change': {'actions': ['no-op'], 'before': rows[address], 'after': rows[address]}})
        return {'resource_changes': changes, 'variables': {k: {'value': v} for k, v in candidate.items()}}

    def routes(self, url_map):
        return any(self.request['hostname'] in rule['hosts'] for rule in url_map.get('host_rule') or [])

    def native(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == 'gcloud':
            self.assertIn('--project=' + self.values['project_id'], argv)
            return '[]'
        if argv[0] == 'aws':
            if 'get-caller-identity' in argv:
                return json.dumps({'Account': self.values['account_id']})
            return json.dumps({'TargetGroups': [], 'Rules': [], 'SecurityGroupRules': [], 'ResourceRecordSets': []})
        work, command = Path(argv[1].split('=', 1)[1]), argv[2]
        self.assertEqual(kwargs, {'timeout': 900} if command == 'apply' else {})
        if command == 'init':
            self.assertEqual(json.loads((work / 'backend.tf.json').read_text())['terraform']['backend']['local']['path'], self.config['state_file'])
            return ''
        if command == 'plan':
            variables = next(a.split('=', 1)[1] for a in argv if a.startswith('-var-file='))
            path = next(a.split('=', 1)[1] for a in argv if a.startswith('-out='))
            document = self.document(cleanup.read_private(variables))
            if self.mutate_plan and Path(variables).name == 'change.json':
                self.mutate_plan(document)
            if Path(variables).stem in self.mutations:
                self.mutations[Path(variables).stem](document)
            if self.after_drift and self.applied:
                document['resource_drift'] = [{'address': 'google_compute_global_address.app'}]
            self.documents[path] = document
            cleanup.durable_write(path, b'fake-saved-plan-' + Path(path).name.encode())
            return ''
        if command == 'show':
            return json.dumps(self.documents[argv[-1]])
        self.assertEqual(command, 'apply')
        self.assertEqual(cleanup.read_private(work / 'intent.json')['phase'], 'applying')
        if self.fail_apply in (True, Path(argv[-1]).name):
            raise RuntimeError('uncertain fake provider response')
        state = cleanup.read_private(self.config['state_file']); rows = state_rows(state)
        changed = [(item['address'], item['change']['actions']) for item in self.documents[argv[-1]]['resource_changes']
                   if item['change']['actions'] not in (['no-op'], ['read'])]
        self.applies.append((Path(argv[-1]).name, changed))
        # GCP: a backend still referenced by a live URL map cannot be deleted. One plan does
        # not order the URL-map update first, so model the observed provider rejection.
        if any(address.startswith('google_compute_backend_service.routes[') and actions == ['delete']
               for address, actions in changed) and any(self.routes(rows[address]) for address in cleanup.DETACH):
            raise RuntimeError('Error 400: resourceInUseByAnotherResource')
        for item in self.documents[argv[-1]]['resource_changes']:
            action = item['change']['actions']
            if action == ['delete']:
                del rows[item['address']]
            elif action in (['update'], ['create']):
                rows[item['address']] = copy.deepcopy(item['change']['after'])
                rows[item['address']]['id'] = rows[item['address']].get('id') or item['address'] + '-restored-id'
        state['resources'] = state_resources(rows); state['serial'] += 1
        self.write(self.config['state_file'], state)
        # Provider refresh after apply should use the candidate as the effective state.
        self.applied = True
        return ''

    def test_stop_start_delete_preserve_shared_ids_and_certificates(self):
        original = gcp_routes.owned(self.state)
        plan = cleanup.plan(self.binding, 'stop')
        self.assertEqual(sum(r['actions'] == ['delete'] for r in plan['private']['native']['changes']), 4)
        receipt = cleanup.execute(self.binding, 'stop', plan)
        self.assertEqual(receipt['phase'], 'stopped')
        calls = list(self.calls)
        self.assertEqual(cleanup.execute(self.binding, 'stop', plan), receipt)
        self.assertEqual(self.calls, calls)
        with self.assertRaises(ValueError):
            cleanup.execute(self.binding, 'delete', plan)
        stopped = gcp_routes.owned(cleanup.read_private(self.config['state_file']))
        for kind in gcp_routes.KINDS[4:]:
            address = kind + '.routes[' + json.dumps(self.request['application_id']) + ']'
            self.assertEqual(stopped[address], original[address])
        resumed = cleanup.plan(self.binding, 'start')
        self.assertEqual(cleanup.execute(self.binding, 'start', resumed)['phase'], 'started')
        deleting = cleanup.plan(self.binding, 'delete')
        self.assertEqual(sum(r['actions'] == ['delete'] for r in deleting['private']['native']['changes']), 7)
        self.assertEqual(cleanup.execute(self.binding, 'delete', deleting)['phase'], 'deleted')
        after = gcp_routes.owned(cleanup.read_private(self.config['state_file']))
        self.assertEqual(after['google_compute_global_address.app'], 'shared-public-ip')
        self.assertEqual(len(after), 4)
        with self.assertRaisesRegex(ValueError, 'stopped'):
            cleanup.plan(self.binding, 'start')

    def backend(self):
        return 'google_compute_backend_service.routes[' + json.dumps(self.request['application_id']) + ']'

    def test_gcp_detaches_both_url_maps_in_a_reviewed_apply_before_deleting_the_backend(self):
        for action in ('stop', 'delete'):
            with self.subTest(action=action):
                self.applies.clear()
                plan = cleanup.plan(self.binding, action)
                self.assertEqual(plan['private']['native']['detach']['candidate']['detached_routes'], [self.request['application_id']])
                self.assertEqual(cleanup.execute(self.binding, action, plan)['phase'], {'stop': 'stopped', 'delete': 'deleted'}[action])
                (first, detached), (second, removed) = self.applies
                self.assertEqual((first, second), ('detach.tfplan', 'remove.tfplan'))
                self.assertEqual({address for address, _ in detached}, cleanup.DETACH)
                self.assertTrue(all(actions == ['update'] for _, actions in detached))
                self.assertIn((self.backend(), ['delete']), removed)
                self.assertFalse({address for address, _ in removed} & cleanup.DETACH)
                if action == 'stop':
                    self.assertEqual(cleanup.execute(self.binding, 'start', cleanup.plan(self.binding, 'start'))['phase'], 'started')

    def test_single_plan_removal_reproduces_the_backend_in_use_failure_and_fails_closed(self):
        with patch('application_cleanup.staged', return_value=False):
            plan = cleanup.plan(self.binding, 'delete')
            self.assertNotIn('detach', plan['private']['native'])
            with self.assertRaises(cleanup.CleanupError) as caught:
                cleanup.execute(self.binding, 'delete', plan)
        self.assertTrue(caught.exception.unknown)
        self.assertEqual([name for name, _ in self.applies], ['change.tfplan'])
        self.assertEqual(cleanup.read_private(self.config['variables_file']), self.values)

    def test_reviewed_plan_without_required_detach_stage_is_rejected_before_any_write(self):
        with patch('application_cleanup.staged', return_value=False):
            plan = cleanup.plan(self.binding, 'delete')
        with self.assertRaisesRegex(ValueError, 'detach stage'):
            cleanup.execute(self.binding, 'delete', plan)
        self.assertEqual(self.applies, [])
        self.assertEqual(cleanup.read_private(Path(plan['private']['native']['work']) / 'intent.json')['phase'], 'planned')

    def test_detach_plan_rejects_unrelated_routes_deletes_and_partial_detachment(self):
        def foreign_host(document):
            row = next(r for r in document['resource_changes'] if r['address'] == 'google_compute_url_map.app')
            row['change']['after']['host_rule'] = []  # also removes the existing legacy host
        def early_delete(document):
            row = next(r for r in document['resource_changes'] if r['address'] == self.backend())
            row['change'] = {'actions': ['delete'], 'before': row['change']['before'], 'after': None}
        def firewall(document):
            row = next(r for r in document['resource_changes'] if r['address'] == 'google_compute_firewall.gfe')
            after = copy.deepcopy(row['change']['before']); after['allow'][0]['ports'] = after['allow'][0]['ports'][:-1]
            row['change'] = {'actions': ['update'], 'before': row['change']['before'], 'after': after}
        def redirect_kept(document):
            row = next(r for r in document['resource_changes'] if r['address'] == 'google_compute_url_map.redirect')
            row['change'] = {'actions': ['no-op'], 'before': row['change']['before'], 'after': row['change']['before']}
        for mutation in (foreign_host, early_delete, firewall, redirect_kept):
            with self.subTest(mutation=mutation.__name__):
                self.mutations['detach'] = mutation
                with self.assertRaises(ValueError):
                    cleanup.plan(self.binding, 'delete')
                self.assertEqual(self.applies, [])
        self.assertEqual(cleanup.read_private(self.config['variables_file']), self.values)

    def test_interruption_or_mismatch_after_any_stage_fails_closed_and_records_the_stage(self):
        def keeps_url_map(document):  # refresh shows the live URL map still routes the app
            row = next(r for r in document['resource_changes'] if r['address'] == 'google_compute_url_map.app')
            row['change']['actions'] = ['update']
        def drops_backend_delete(document):  # runtime removal must equal the reviewed deletions
            document['resource_changes'] = [r for r in document['resource_changes'] if r['address'] != self.backend()]
        cases = (('detach apply', {'fail_apply': 'detach.tfplan'}, 'detaching', []),
                 ('detach unverified', {'detached': keeps_url_map}, 'detaching', ['detach.tfplan']),
                 ('removal differs', {'remove': drops_backend_delete}, 'detached', ['detach.tfplan']),
                 ('remove apply', {'fail_apply': 'remove.tfplan'}, 'removing', ['detach.tfplan']))
        for name, setup, stage, applied in cases:
            with self.subTest(name=name):
                self.setUp()
                self.fail_apply = setup.get('fail_apply', False)
                self.mutations = {k: v for k, v in setup.items() if k != 'fail_apply'}
                plan = cleanup.plan(self.binding, 'delete')
                with self.assertRaises(cleanup.CleanupError) as caught:
                    cleanup.execute(self.binding, 'delete', plan)
                self.assertTrue(caught.exception.unknown)
                self.assertEqual([n for n, _ in self.applies], applied)
                intent = cleanup.read_private(Path(plan['private']['native']['work']) / 'intent.json')
                self.assertEqual((intent['phase'], intent['stage']), ('unknown', stage))
                calls = len(self.calls)
                with self.assertRaisesRegex(ValueError, 'reconciliation'):
                    cleanup.execute(self.binding, 'delete', plan)
                self.assertEqual(len(self.calls), calls, 'an uncertain stage is never replayed')
                self.assertEqual(cleanup.read_private(self.config['variables_file']), self.values)

    def test_partial_apply_cannot_be_replayed_or_bypassed_by_new_plan(self):
        plan = cleanup.plan(self.binding, 'delete'); self.fail_apply = True
        with self.assertRaises(cleanup.CleanupError) as caught:
            cleanup.execute(self.binding, 'delete', plan)
        self.assertTrue(caught.exception.unknown)
        calls = len(self.calls)
        for run in (lambda: cleanup.execute(self.binding, 'delete', plan), lambda: cleanup.plan(self.binding, 'stop')):
            with self.assertRaisesRegex(ValueError, 'reconciliation'):
                run()
        self.assertEqual(calls, len(self.calls))
        self.assertEqual(cleanup.read_private(self.config['variables_file']), self.values)

    def test_gcp_refresh_accepts_unset_descriptions_but_rejects_route_drift(self):
        address = 'google_compute_url_map.redirect'
        before = {'id': 'projects/fixture/global/urlMaps/redirect',
                  'host_rule': [{'description': None, 'hosts': ['one.example.com'], 'path_matcher': 'one'}]}
        after = copy.deepcopy(before)
        after['host_rule'][0]['description'] = ''
        base = {'resource_drift': [{'address': address, 'type': 'google_compute_url_map', 'mode': 'managed',
                'change': {'actions': ['update'], 'before': before, 'after': after}}],
                'resource_changes': [{'address': address, 'change': {'actions': ['no-op'], 'before': after, 'after': after}}]}
        cleanup.unchanged(base)
        for key, value in [('hosts', ['foreign.example.com']), ('description', 'changed'), ('path_matcher', 'foreign')]:
            with self.subTest(key=key):
                changed = copy.deepcopy(base)
                changed['resource_drift'][0]['change']['after']['host_rule'][0][key] = value
                with self.assertRaises(ValueError):
                    cleanup.unchanged(changed)

    def test_gcp_lifecycle_accepts_only_equivalent_provider_routing_representations(self):
        document = self.document({**self.values, 'routes': {}})
        for row in document['resource_changes']:
            address = row['address']
            if not address.startswith('google_compute_url_map.'):
                continue
            before, after = copy.deepcopy(row['change']['before']), copy.deepcopy(row['change']['after'])
            foreign = {'hosts': ['another.railshot.io'], 'path_matcher': 'another'}
            backend = 'projects/fixture-project/global/backendServices/another'
            for value in (before, after):
                value['host_rule'].append(copy.deepcopy(foreign))
                value['path_matcher'].append({'name': 'another', 'default_service': backend})
                for block in value['host_rule'] + value['path_matcher']:
                    block['description'] = '' if value is before else None
            before['path_matcher'][-1]['default_service'] = 'https://www.googleapis.com/compute/v1/' + backend
            for action in ('delete', 'stop', 'start'):
                left, right = (after, before) if action == 'start' else (before, after)
                with self.subTest(address=address, action=action):
                    cleanup.validate_shared('gcp', address, left, right, {},
                        self.values['routes'][self.request['application_id']], self.request['application_id'], self.values, action)
            changed = copy.deepcopy(after)
            changed['host_rule'][-1]['hosts'] = ['different.railshot.io']
            with self.assertRaises(ValueError):
                cleanup.validate_shared('gcp', address, before, changed, {},
                    self.values['routes'][self.request['application_id']], self.request['application_id'], self.values, 'delete')

    def test_stale_serial_source_plan_and_foreign_dns_fail_before_apply(self):
        plan = cleanup.plan(self.binding, 'delete')
        for mutation in ('serial', 'saved', 'dns'):
            with self.subTest(mutation=mutation):
                state = copy.deepcopy(self.state)
                if mutation == 'serial':
                    state['serial'] += 1
                self.write(self.config['state_file'], state)
                saved = Path(plan['private']['native']['work']) / 'change.tfplan'
                saved.write_bytes(b'tampered' if mutation == 'saved' else b'fake-saved-plan-change.tfplan')
                context = patch('dns.application_snapshot', return_value=[{'foreign': True}]) if mutation == 'dns' else patch('dns.records_at', return_value=[])
                with context, self.assertRaises(ValueError):
                    cleanup.execute(self.binding, 'delete', plan)
                self.assertFalse(any(call[2] == 'apply' for call in self.calls))

    def test_shared_lb_delete_replacement_unrelated_egress_and_unknown_are_rejected(self):
        def mutate(document, mode):
            change = document['resource_changes'][-1]['change']
            if mode == 'foreign-delete':
                change.update(actions=['delete'], after=None)
            elif mode == 'replace':
                document['resource_changes'][0]['change']['actions'] = ['delete', 'create']
            elif mode == 'unknown':
                document['resource_changes'][-2]['change']['after_unknown'] = {'source_ranges': True}
            else:
                document['resource_changes'][-2]['change']['after']['source_ranges'] = ['0.0.0.0/0']
        for mode in ('foreign-delete', 'replace', 'unknown', 'firewall'):
            self.mutate_plan = lambda document: mutate(document, mode)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                cleanup.plan(self.binding, 'delete')
        self.assertFalse(any(call[2] == 'apply' for call in self.calls))

    def test_drift_after_apply_is_unknown_and_does_not_commit_variables(self):
        plan = cleanup.plan(self.binding, 'stop'); self.after_drift = True
        with self.assertRaises(cleanup.CleanupError):
            cleanup.execute(self.binding, 'stop', plan)
        self.assertEqual(cleanup.read_private(self.config['variables_file']), self.values)


class AWSLifecycleTest(unittest.TestCase):
    write = LifecycleTest.write
    native = LifecycleTest.native

    def setUp(self):
        LifecycleTest.setUp(self)
        self.binding['provider'] = 'aws'
        self.key = 'app-' + '7' * 24
        self.route = {'host': self.binding['hostname'], 'node_port': self.request['node_port'], 'health_path': '/ready',
                      'provider_kind': 'aws', 'target_private_ip': '10.1.0.2', 'priority': 1000,
                      'target_security_group_id': 'sg-0123456789abcdef0', 'manage_dns': False}
        self.values = {'base_domain': 'railshot.io', 'region': 'test-region', 'account_id': '123456789012',
                       'vpc_id': 'vpc-0123456789abcdef0', 'routes': {self.key: self.route}}
        tfdir = self.root / 'module'; (tfdir / '.terraform').mkdir(parents=True)
        self.write(tfdir / '.terraform/terraform.tfstate', {'backend': {'type': 'local', 'config': {'path': str(self.root / 'state.json')}}})
        self.config = {'version': 1, 'state_dir': str(self.root / 'edge'), 'terraform_dir': str(tfdir),
                       'variables_file': str(self.root / 'vars.json'), 'auto_apply': True}
        # Existing creation writers leave the reservation ledger at reserved, and
        # update only the app file to applied. Lifecycle must understand that.
        Path(self.config['state_dir']).mkdir(mode=0o700)
        row = {'route_key': self.key, 'phase': 'reserved', 'config_sha256': cleanup.digest(self.config), 'route': self.route,
               'request': {'target_id': self.binding['application_id'], 'environment_id': self.binding['environment_id'],
                           'app': self.binding['registered']['app'], 'tenant': self.binding['registered']['tenant'],
                           'namespace': self.binding['application_id']}}
        self.write(Path(self.config['state_dir']) / 'allocations.json', {self.key: row})
        self.write(Path(self.config['state_dir']) / (self.key + '.json'), {**row, 'phase': 'applied'})
        self.config['state_file'] = str(self.root / 'state.json')  # test runner access only
        self.write(self.config_path, {k: v for k, v in self.config.items() if k != 'state_file'})
        self.write(self.config['variables_file'], self.values)
        suffix = '.app[' + json.dumps(self.key) + ']'
        self.definitions = {
            'aws_lb_target_group' + suffix: {'vpc_id': self.values['vpc_id'], 'port': self.route['node_port'], 'protocol': 'HTTP',
                                           'target_type': 'ip', 'health_check': [{'path': '/ready'}], 'arn': 'exact-owned-target-group'},
            'aws_lb_target_group_attachment' + suffix: {'target_id': '10.1.0.2', 'port': self.route['node_port'],
                                                       'target_group_arn': 'exact-owned-target-group'},
            'aws_lb_listener_rule' + suffix: {'priority': 1000, 'listener_arn': 'existing-shared-listener',
                                             'condition': [{'host_header': [{'values': [self.route['host']]}]}]},
            'aws_security_group_rule.target_from_alb[' + json.dumps(self.route['target_security_group_id'] + ':' + str(self.route['node_port'])) + ']': {
                'security_group_id': self.route['target_security_group_id'], 'type': 'ingress', 'protocol': 'tcp',
                'from_port': self.route['node_port'], 'to_port': self.route['node_port'], 'source_security_group_id': 'shared-sg'}}
        self.egress = {'from_port': self.route['node_port'], 'to_port': self.route['node_port'], 'protocol': 'tcp',
                       'cidr_blocks': ['10.1.0.2/32'], 'description': '', 'ipv6_cidr_blocks': [],
                       'prefix_list_ids': [], 'security_groups': [], 'self': False}
        rows = {k: {**v, 'id': k + '-original'} for k, v in self.definitions.items()}
        rows.update({'aws_lb.app': {'id': 'shared-alb'}, 'aws_security_group.alb': {
            'id': 'shared-sg', 'egress': [self.egress], 'ingress': [{'from_port': 443}]}})
        self.state = {'version': 4, 'lineage': 'original-lineage', 'serial': 10, 'resources': state_resources(rows)}
        self.write(self.config['state_file'], self.state)

    def document(self, candidate):
        rows = state_rows(cleanup.read_private(self.config['state_file']))
        route = candidate['routes'].get(self.key)
        active = route is not None and route.get('enabled', True)
        changes = []
        for address, definition in self.definitions.items():
            if active and address not in rows:
                change = {'actions': ['create'], 'before': None, 'after': definition}
            elif not active and address in rows:
                change = {'actions': ['delete'], 'before': rows[address], 'after': None}
            elif address in rows:
                change = {'actions': ['no-op'], 'before': rows[address], 'after': rows[address]}
            else:
                continue
            changes.append({'address': address, 'change': change})
        for address in ('aws_security_group.alb', 'aws_lb.app'):
            after = copy.deepcopy(rows[address])
            if address.endswith('.alb'):
                after['egress'] = [self.egress] if active else []
            changes.append({'address': address, 'change': {'actions': ['no-op'] if after == rows[address] else ['update'],
                                                           'before': rows[address], 'after': after}})
        return {'variables': {k: {'value': v} for k, v in candidate.items()}, 'resource_changes': changes}

    def test_stop_start_delete_last_app_from_real_writer_ledger_shape(self):
        for action, phase in (('stop', 'stopped'), ('start', 'started'), ('delete', 'deleted')):
            plan = cleanup.plan(self.binding, action)
            before = cleanup.read_private(self.config['state_file'])
            cleanup.validate(self.binding, action, plan)
            self.assertEqual(cleanup.read_private(self.config['state_file']), before)
            self.assertEqual(cleanup.execute(self.binding, action, plan)['phase'], phase)
        self.assertEqual(cleanup.read_private(self.config['variables_file'])['routes'], {})
        rows = state_rows(cleanup.read_private(self.config['state_file']))
        self.assertEqual(set(rows), {'aws_lb.app', 'aws_security_group.alb'})
        self.assertEqual(rows['aws_lb.app']['id'], 'shared-alb')
        self.assertEqual(rows['aws_security_group.alb']['ingress'], [{'from_port': 443}])

    def refreshed_document(self, candidate):
        document = self.document(candidate)
        document['resource_drift'] = []
        for row in document['resource_changes']:
            kind = row['address'].split('.')[0]
            change = row['change']
            if kind not in ('aws_lb_listener_rule', 'aws_lb_target_group') or not change.get('before'):
                continue
            stale = {**change['before'], 'tags': None}
            observed = {**stale, 'tags': {}}
            if kind == 'aws_lb_target_group':
                stale['load_balancer_arns'] = []
                observed['load_balancer_arns'] = ['shared-alb']
            document['resource_drift'].append({'address': row['address'], 'type': kind, 'mode': 'managed',
                'change': {'actions': ['update'], 'before': stale, 'after': observed}})
            change['before'] = observed
            if change['actions'] == ['no-op']:
                change['after'] = observed
        return document

    def test_aws_metadata_refresh_does_not_block_stop_start_delete(self):
        original = self.document
        refreshed = self.refreshed_document
        def observed(candidate):
            with patch.object(self, 'document', original):
                return refreshed(candidate)
        with patch.object(self, 'document', side_effect=observed):
            self.test_stop_start_delete_last_app_from_real_writer_ledger_shape()

    def test_refresh_still_rejects_configuration_identity_and_foreign_alb_drift(self):
        base = self.refreshed_document(self.values)
        cleanup.unchanged(base)
        mutations = {
            'tag value': lambda row: row['change']['after'].update(tags={'Owner': 'someone-else'}),
            'port': lambda row: row['change']['after'].update(port=9999),
            'identity': lambda row: row['change']['after'].update(id='foreign-resource'),
            'foreign ALB': lambda row: row['change']['after'].update(load_balancer_arns=['other-alb']),
            'second ALB': lambda row: row['change']['after'].update(load_balancer_arns=['shared-alb', 'other-alb']),
            'replacement': lambda row: row['change'].update(replace_paths=[['port']]),
            'import': lambda row: row['change'].update(importing={'id': 'foreign-resource'}),
            'deposed': lambda row: row.update(deposed='old-instance'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                document = copy.deepcopy(base)
                drift = next(row for row in document['resource_drift'] if row['type'] == 'aws_lb_target_group')
                mutate(drift)
                planned = next(row['change'] for row in document['resource_changes'] if row['address'] == drift['address'])
                planned['before'] = planned['after'] = copy.deepcopy(drift['change']['after'])
                with self.assertRaises(ValueError):
                    cleanup.unchanged(document)

    def test_implicit_backend_and_next_writer_use_current_bundle_not_old_module(self):
        import edge
        config = cleanup.read_private(self.config_path)
        directory = Path(config['terraform_dir'])
        (directory / '.terraform/terraform.tfstate').unlink()
        self.write(directory / 'terraform.tfstate', self.state)
        (directory / 'main.tf').write_text('locals { old_module_ignores_enabled = true }')
        self.assertEqual(edge.local_state_file(config), str(directory / 'terraform.tfstate'))
        calls = []
        with patch('edge.native', side_effect=lambda argv: calls.append(argv) or '{}'):
            edge.terraform(config, 'plan', '-var-file=' + self.config['variables_file'])
        work = Path(calls[-1][1].split('=', 1)[1])
        self.assertNotEqual(work, directory)
        self.assertIn('local.active_routes', (work / 'main.tf').read_text())
        self.assertIn('old_module_ignores_enabled', (directory / 'main.tf').read_text())
        self.assertEqual(json.loads((work / 'backend.tf.json').read_text())['terraform']['backend']['local']['path'],
                         str(directory / 'terraform.tfstate'))

    def test_provider_presence_after_delete_cannot_be_reported_as_success(self):
        plan = cleanup.plan(self.binding, 'delete')
        native = self.native
        def retained_group(argv, **kwargs):
            if 'describe-target-groups' in argv:
                return json.dumps({'TargetGroups': [{'TargetGroupArn': 'exact-owned-target-group'}]})
            return native(argv, **kwargs)
        with patch('edge.native', side_effect=retained_group), self.assertRaises(cleanup.CleanupError) as caught:
            cleanup.execute(self.binding, 'delete', plan)
        self.assertTrue(caught.exception.unknown)
        self.assertEqual(cleanup.read_private(self.config['variables_file']), self.values)


class DNSDeleteTest(unittest.TestCase):
    def test_exact_id_and_owner_preflight_all_records_then_delete_and_readback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); config = {'version': 1, 'zone_id': 'a' * 32, 'base_domain': 'example.com',
                'token_file': str(root / 'token'), 'state_dir': str(root)}
            config['_sha256'] = dns.digest(config)
            requests = [{'application_id': 'app-123', 'hostname': 'demo.example.com', 'type': 'A', 'content': '34.1.2.3'},
                        {'application_id': 'app-123', 'purpose': 'certificate', 'application_hostname': 'demo.example.com',
                         'hostname': '_acme-challenge_a.demo.example.com', 'type': 'CNAME', 'content': 'a.authorize.certificatemanager.goog'}]
            records = {}
            for n, request in enumerate(requests):
                record = {'id': str(n + 1) * 32, 'name': request['hostname'], 'type': request['type'], 'content': request['content'],
                          'comment': 'railshot:app-123', 'proxied': False, 'ttl': 300}
                records[request['hostname']] = [record]
                key = dns.digest({'zone_id': config['zone_id'], 'hostname': request['hostname']})
                dns.save(root / (key + '.json'), {'phase': 'verified', 'request': request,
                    'config_sha256': config['_sha256'], 'receipt': {'record_id': record['id']}})
            calls = []
            def transport(config, method, *, record_id=None):
                calls.append((method, record_id))
                self.assertEqual(method, 'DELETE')
                self.assertTrue(any(dns.private_json(p).get('phase') == 'deleting' for p in root.glob('*.json')))
                for name, rows in records.items():
                    if rows and rows[0]['id'] == record_id:
                        records[name] = []
            with patch('dns.records_at', side_effect=lambda config, host: copy.deepcopy(records.get(host, []))), patch('dns.transport', side_effect=transport):
                expected = dns.application_snapshot(config, 'app-123', 'demo.example.com')
                records[requests[1]['hostname']][0]['id'] = 'f' * 32
                with self.assertRaises(dns.DNSError):
                    dns.remove_application(config, 'app-123', 'demo.example.com', expected)
                self.assertFalse(calls)
                records[requests[1]['hostname']][0]['id'] = '2' * 32
                dns.remove_application(config, 'app-123', 'demo.example.com', expected)
                self.assertEqual(set(calls), {('DELETE', '1' * 32), ('DELETE', '2' * 32)})
                self.assertEqual(dns.application_snapshot(config, 'app-123', 'demo.example.com'), [])


if __name__ == '__main__':
    unittest.main()
