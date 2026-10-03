import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner import repair_evidence
from diagnostics import Diagnostics
from source_snapshot import capture, entries_digest
from execution import GATE_ORDER, RELEASE_ORDERS


def case_fixture(ws, run, layers=GATE_ORDER, scope='packaging'):
    run.mkdir(exist_ok=True)
    source = entries_digest(capture(ws))
    d = Diagnostics(ws, run, 'fixture-run', 'fixture-run:0', layers, scope, source)
    d.capture()
    d.finish({'source_sha256': source, 'status': 'FAIL', 'release_eligible': False,
              'layers': [{'layer': 'L2', 'outcome': 'FAIL'}],
              'failure': {'layer': 'L2', 'class': 'F1', 'signature': 'failure-fixture', 'excerpt': 'Dependency install failed'}})
    return repair_evidence.load(run)


def bind_proposal(run, output):
    data = repair_evidence.load(run); case = json.loads(data)
    return {**output, 'addresses_failure': case['failure']['fingerprint'],
            'evidence_binding': {'case_id': case['case_id'], 'case_sha256': repair_evidence.sha(data),
               'source_sha256': case['source']['tested_sha256'], 'policy_sha256': case['policy_sha256']},
            'evidence_refs': [{'kind': 'log', 'id': 'failure', 'sha256': repair_evidence.sha(case['failure']['excerpt'].encode())}]}


class RepairEvidenceTest(unittest.TestCase):
    def test_real_source_and_case_required_before_any_proposal_write(self):
        with tempfile.TemporaryDirectory() as directory:
            ws, run = Path(directory)/'work', Path(directory)/'run'; ws.mkdir()
            (ws/'app.py').write_text('print(1)\n')
            raw = case_fixture(ws, run)
            output = bind_proposal(run, {'files': [{'path':'app.py', 'content':'print(2)\n'}],
                'root_cause':'app.py:1 prints a constant', 'assumptions':[]})
            check = lambda proposal: repair_evidence.verify(run, ws, proposal, GATE_ORDER, 'packaging', raw)
            self.assertFalse(check(output)['causal_claim_verified'])
            for key, value in [('root_cause','nonexistent.py:999'), ('assumptions',['app.py:999']),
                               ('addresses_failure','unrelated-case-signature'), ('evidence_refs',[]),
                               ('evidence_binding', {**output['evidence_binding'], 'source_sha256':'0'*64}),
                               ('evidence_refs',[{'kind':'source','path':'app.py','line':1,'sha256':'0'*64}])]:
                with self.subTest(key=key), self.assertRaises(ValueError): check({**output,key:value})
            (ws/'app.py').write_text('print(3)\n')
            with self.assertRaises(ValueError): check(output)
            (ws/'app.py').write_text('print(1)\n')
            (run/'diagnostics/case.json').write_bytes(raw+b' ')
            with self.assertRaises(ValueError): check(output)
            self.assertEqual((ws/'app.py').read_text(),'print(1)\n')

    def test_initial_context_is_bounded_and_retains_original_reference_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            ws, run = Path(directory)/'work', Path(directory)/'run'; ws.mkdir()
            (ws/'app.py').write_text('print(1)\n')
            case = json.loads(case_fixture(ws, run))
            case['failure']['excerpt'] = '실패 TS2322 ' * 2000
            case['source']['files']['irrelevant.txt'] = {'sha256': 'a'*64, 'bytes': 999, 'lines': 5}
            raw = json.dumps(case).encode()
            context = repair_evidence.context(raw)
            self.assertLessEqual(len(context['failure']['excerpt'].encode()), 3000)
            self.assertNotIn('irrelevant.txt', json.dumps(context))
            self.assertEqual(context['stage'], 'image.build')
            self.assertEqual(context['evidence_binding']['case_sha256'], repair_evidence.sha(raw))
            self.assertEqual(context['failure_reference']['sha256'], repair_evidence.sha(case['failure']['excerpt'].encode()))
            self.assertGreater(context['failure']['excerpt_omitted_bytes'], 0)

    def test_all_profiles_bind_exact_plan_and_policy(self):
        from runner.run_agent import record_plan, with_files
        for order in RELEASE_ORDERS:
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                ws, run = Path(directory)/'work', Path(directory)/'run'; ws.mkdir(); (ws/'app.py').write_text('print(1)\n')
                raw = case_fixture(ws, run, order)
                output = bind_proposal(run, {'status':'proposed', 'root_cause':'app.py:1',
                    'files':[{'path':'app.py','content':'print(2)\n'}], 'files_changed':[{'path':'app.py','why':'fixture'}],
                    'gate_plan':[{'gate':layer,'action':'verify'} for layer in order]})
                record_plan(run,'fixer',output,order,workspace=ws,expected=raw)
                self.assertTrue(json.loads((run/'fixer-plan.json').read_text())['evidence']['reference_integrity_verified'])
                schema=with_files(json.loads((Path(__file__).resolve().parents[1]/'schemas/report.schema.json').read_text()), ['**'], order)
                self.assertEqual(schema['properties']['gate_plan']['minItems'],len(order))
                with self.assertRaises(ValueError): repair_evidence.verify(run,ws,output,order,'source',raw)

if __name__ == '__main__': unittest.main()
