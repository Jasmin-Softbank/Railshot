"""Run: python3 -m unittest discover -s platform/infra -p test_costs.py"""
import tempfile
import unittest
import json
import copy
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

from costs import import_snapshot, ledger, normalize, report, reserve


class CostsTest(unittest.TestCase):
    def aws_snapshot(self):
        return {'account_id': '123456789012', 'request': {
            'TimePeriod': {'Start': '2026-10-01', 'End': '2026-10-02'},
            'Granularity': 'DAILY', 'Metrics': ['UnblendedCost'],
            'Filter': {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': ['123456789012']}}},
            'response': {'ResultsByTime': [{'TimePeriod': {'Start': '2026-10-01', 'End': '2026-10-02'},
                'Total': {'UnblendedCost': {'Amount': '0', 'Unit': 'USD'}}, 'Groups': [], 'Estimated': True}]}}

    def test_aws_explicit_zero_keeps_estimate_and_account_binding(self):
        now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        snapshot = self.aws_snapshot()
        with closing(ledger(':memory:')) as db:
            imported = import_snapshot(db, 'aws/2026-10', 'aws', '2026-10', now.isoformat(),
                                       snapshot, now=now, source_scope='123456789012')
            self.assertEqual(imported['basis'], 'reported_estimate')
            current = report(db, 'aws/2026-10', now=now)
            self.assertEqual(current['totals'], {'USD': '0'})
            self.assertEqual(current['basis'], 'reported_estimate')
            self.assertEqual(current['scope_binding'], 'query_verified')
            held = reserve(db, scope='aws/2026-10', operation_id='aws-apply', currency='USD',
                           incremental_cost='3', limit='10', unreported_cost='2', now=now)
            self.assertEqual(held['projected'], '5')
            with self.assertRaisesRegex(ValueError, 'stale'):
                reserve(db, scope='aws/2026-10', operation_id='later', currency='USD',
                        incremental_cost='3', limit='10', unreported_cost='2', now=now + timedelta(days=2))
            with self.assertRaisesRegex(ValueError, 'scope mismatch'):
                import_snapshot(db, 'wrong-account', 'aws', '2026-10', now.isoformat(),
                                snapshot, now=now, source_scope='999999999999')

    def test_aws_rejects_incomplete_filtered_unbound_and_stale_snapshots(self):
        now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        variants = []
        for mutation in ('bare', 'empty', 'page', 'account', 'service', 'group', 'metric', 'gap', 'estimated', 'nan', 'old'):
            value = copy.deepcopy(self.aws_snapshot())
            if mutation == 'bare': value = value['response']
            elif mutation == 'empty': value['response']['ResultsByTime'] = []
            elif mutation == 'page': value['response']['NextPageToken'] = 'unfinished'
            elif mutation == 'account': value['request']['Filter']['Dimensions']['Values'] = ['999999999999']
            elif mutation == 'service': value['request']['Filter']['Dimensions']['Key'] = 'SERVICE'
            elif mutation == 'group': value['response']['ResultsByTime'][0]['Groups'] = [{'Keys': ['service']}]
            elif mutation == 'metric': value['response']['ResultsByTime'][0]['Total'] = {}
            elif mutation == 'gap': value['request']['TimePeriod']['End'] = '2026-10-03'
            elif mutation == 'estimated': del value['response']['ResultsByTime'][0]['Estimated']
            elif mutation == 'nan': value['response']['ResultsByTime'][0]['Total']['UnblendedCost']['Amount'] = 'NaN'
            variants.append((mutation, value, now + timedelta(days=1) if mutation == 'old' else now))
        with closing(ledger(':memory:')) as db:
            for name, value, observation in variants:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    import_snapshot(db, name, 'aws', '2026-10', observation.isoformat(),
                                    value, now=observation, source_scope='123456789012')
            with self.assertRaisesRegex(ValueError, 'source scope required'):
                import_snapshot(db, 'unbound', 'aws', '2026-10', now.isoformat(), self.aws_snapshot(), now=now)

    def test_aws_monthly_total_and_cli_import(self):
        now = datetime.now(timezone.utc)
        first = now.date().replace(day=1)
        end = now.date() + timedelta(days=1)
        snapshot = self.aws_snapshot()
        interval = {'Start': first.isoformat(), 'End': end.isoformat()}
        snapshot['request'].update(Granularity='MONTHLY', TimePeriod=interval)
        snapshot['response']['ResultsByTime'][0].update(TimePeriod=interval)
        snapshot['response']['ResultsByTime'][0]['Total']['UnblendedCost']['Amount'] = '1.25'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'ce.json').write_text(json.dumps(snapshot))
            command = [sys.executable, str(Path(__file__).with_name('costs.py')), '--db', str(path / 'cost.sqlite')]
            result = subprocess.run(command + ['import', '--provider', 'aws', '--scope', 'aws-current',
                '--period', now.strftime('%Y-%m'), '--observed-at', now.isoformat(),
                '--source-scope', snapshot['account_id'], str(path / 'ce.json')], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['source_scope'], snapshot['account_id'])
            result = subprocess.run(command + ['report', '--scope', 'aws-current'], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['totals'], {'USD': '1.25'})

    def test_manual_import_report_and_hold_cli(self):
        now = datetime.now(timezone.utc)
        period = now.strftime('%Y-%m')
        with tempfile.TemporaryDirectory() as directory:
            db, export = Path(directory) / 'cost.sqlite', Path(directory) / 'export.json'
            export.write_text(json.dumps([{'usage_start_time': now.isoformat(), 'project': {'id': 'test-project'},
                                          'currency': 'USD', 'cost': '1.25', 'credits': []}]))
            command = [sys.executable, str(Path(__file__).with_name('costs.py')), '--db', str(db)]
            def cli(*args):
                return json.loads(subprocess.run(command + list(args), capture_output=True, text=True, check=True).stdout)
            cli('import', '--provider', 'gcp', '--scope', 'test/' + period, '--period', period,
                '--source-scope', 'test-project', '--observed-at', now.isoformat(), str(export))
            result = cli('report', '--scope', 'test/' + period)
            self.assertEqual(result['totals'], {'USD': '1.25'})
            self.assertEqual(result['freshness'], 'fresh')
            held = cli('reserve', '--scope', 'test/' + period, '--operation-id', 'cli-test', '--currency', 'USD',
                       '--incremental-cost', '1', '--limit', '5', '--unreported-cost', '1')
            self.assertEqual(held['state'], 'held')

    def test_report_reserve_and_fail_closed(self):
        now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        rows = [{'usage_start_time': '2026-10-01T00:00:00Z', 'currency': 'KRW',
                 'project': {'id': 'test-project'}, 'cost': '1200.1', 'credits': [{'amount': '-200.1'}]}]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'costs.sqlite'
            db = ledger(path)
            self.assertEqual(report(db, 'missing')['status'], 'unknown')
            for _ in range(2):
                import_snapshot(db, 'gcp/2026-10', 'gcp', '2026-10', now.isoformat(), rows, now, source_scope='test-project')
            self.assertEqual(report(db, 'gcp/2026-10')['totals'], {'KRW': '1000.0'})
            args = dict(scope='gcp/2026-10', currency='KRW', incremental_cost='3000',
                        limit='5000', unreported_cost='500', now=now)
            # Two concurrent operations cannot both consume the same remaining budget.
            def hold(op):
                conn = ledger(path)
                try:
                    return reserve(conn, operation_id=op, **args)['state']
                except ValueError as e:
                    return str(e)
                finally:
                    conn.close()
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(hold, ['a', 'b']))
            self.assertCountEqual(results, ['held', 'budget exceeded'])
            op = 'a' if results[0] == 'held' else 'b'
            self.assertTrue(reserve(db, operation_id=op, **args)['duplicate'])
            with self.assertRaisesRegex(ValueError, 'conflict'):
                reserve(db, operation_id=op, **{**args, 'incremental_cost': '1'})
            with self.assertRaisesRegex(ValueError, 'stale'):
                reserve(db, operation_id='later', **{**args, 'now': now + timedelta(days=2)})
            with self.assertRaisesRegex(ValueError, 'currency'):
                reserve(db, operation_id='usd', **{**args, 'currency': 'USD'})
            with self.assertRaisesRegex(ValueError, 'older'):
                import_snapshot(db, 'gcp/2026-10', 'gcp', '2026-10', (now-timedelta(hours=1)).isoformat(), rows, now)
            self.assertEqual(report(db, 'gcp/2026-10', now=now + timedelta(days=2))['freshness'], 'stale')
            db.close()
        self.assertEqual(normalize('azure', [{'Date':'10/01/2026', 'BillingCurrency':'USD',
            'ResourceId':'test-only', 'CostInBillingCurrency':'1.25'}], '2026-10')[0]['cost'], '1.25')
        for bad in ([], [{**rows[0], 'cost':'NaN'}], [{**rows[0], 'usage_start_time':'2026-09-01'}]):
            with self.assertRaises(ValueError):
                normalize('gcp', bad, '2026-10')

    def test_azure_official_headers_and_scope_binding(self):
        now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        row = {'Date': '10/01/2026', 'BillingCurrencyCode': 'USD', 'SubscriptionId': 'subscription-1',
               'ResourceId': '/subscriptions/subscription-1/disks/retained', 'CostInBillingCurrency': '1.25'}
        self.assertEqual(normalize('azure', [row], '2026-10')[0]['cost'], '1.25')
        self.assertEqual(normalize('azure', [{k.lower(): v for k,v in row.items()}], '2026-10')[0]['cost'], '1.25')
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            normalize('azure', [{**row, 'BillingCurrency': 'EUR'}], '2026-10')
        with closing(ledger(':memory:')) as db:
            with self.assertRaisesRegex(ValueError, 'scope mismatch'):
                import_snapshot(db, 'azure/2026-10', 'azure', '2026-10', now.isoformat(), [row], now, source_scope='wrong')
            import_snapshot(db, 'azure/2026-10', 'azure', '2026-10', now.isoformat(), [row], now, source_scope='subscription-1')
            self.assertEqual(report(db, 'azure/2026-10', now=now)['scope_binding'], 'row_verified')
            with self.assertRaisesRegex(ValueError, 'conflicting snapshot'):
                import_snapshot(db, 'azure/2026-10', 'azure', '2026-10', now.isoformat(), [{**row, 'CostInBillingCurrency': '2'}], now, source_scope='subscription-1')

    def test_unbound_or_previous_month_cannot_fund_new_operation(self):
        now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        row = {'usage_start_time': '2026-09-30T00:00:00Z', 'currency': 'USD', 'cost': '1', 'project': {'id': 'test-project'}}
        args = dict(operation_id='new', currency='USD', incremental_cost='1', limit='10', unreported_cost='1', now=now)
        with closing(ledger(':memory:')) as db:
            import_snapshot(db, 'unbound', 'gcp', '2026-09', now.isoformat(), [row], now)
            with self.assertRaisesRegex(ValueError, 'scope unverified'):
                reserve(db, scope='unbound', **args)
            import_snapshot(db, 'previous', 'gcp', '2026-09', now.isoformat(), [row], now, source_scope='test-project')
            with self.assertRaisesRegex(ValueError, 'current usage month'):
                reserve(db, scope='previous', **args)

    def test_idempotent_hold_rechecks_snapshot_freshness(self):
        now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        row = {'usage_start_time': now.isoformat(), 'currency': 'USD', 'cost': '1', 'project': {'id': 'test-project'}}
        with closing(ledger(':memory:')) as db:
            import_snapshot(db, 'scope', 'gcp', '2026-10', now.isoformat(), [row], now, source_scope='test-project')
            args = dict(scope='scope', operation_id='held-operation', currency='USD', incremental_cost='1', limit='10', unreported_cost='1')
            reserve(db, **args, now=now)
            from datetime import timedelta
            with self.assertRaisesRegex(ValueError, 'stale'):
                reserve(db, **args, now=now + timedelta(hours=25))


if __name__ == '__main__':
    unittest.main()
