"""Model-free local crash/recovery tests. No Docker, SDK, credentials or cloud access."""
import json
import io
from contextlib import closing, contextmanager, redirect_stdout
import errno
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loop
from run_state import RunState, StateError
import run_state


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.upload = self.root / 'upload'
        self.upload.mkdir()
        (self.upload / 'app.py').write_text('original\n')
        self.run = self.root / 'run'
        self.addCleanup(self.tmp.cleanup)

    def state(self, resume=False, **binding):
        return RunState(self.run, {'source': 'v1', **binding}, resume)

    @contextmanager
    def error(self, code):
        with self.assertRaises(StateError) as caught:
            yield
        self.assertEqual(caught.exception.code, code)

    def cli(self, resume=False, *extra):
        args = ['loop', str(self.upload), str(self.run), *extra]
        if resume:
            args.append('--resume')
        with patch.object(sys, 'argv', args):
            return loop.main()

    @contextmanager
    def gate_result(self, verdict):
        """A synthetic gate producer must leave the same required artifact as the real gate."""
        def produce(ws, run, attempt, *args, **kwargs):
            target = run / f'gate-{attempt}'
            target.mkdir(exist_ok=True)
            (target / 'verdict.json').write_text(json.dumps(verdict))
            return verdict
        with patch.object(loop, 'gate', side_effect=produce) as mocked:
            yield mocked

    @contextmanager
    def agent_result(self, record=None, rc=0, side_effect=None):
        def produce(*args):
            role, _, _, run, attempt = args[:5]
            status, receipt = side_effect(*args) if side_effect else (rc, record)
            (run / f'{role}-{attempt}.json').write_text(json.dumps(receipt))
            if not status:
                (run / f'{role}-{attempt}-events.jsonl').write_text('{}\n')
                (run / f'{role}-{attempt}-session.json').write_text('{}')
            return status, receipt
        with patch.object(loop, 'agent', side_effect=produce) as mocked:
            yield mocked

    def test_real_process_exit_retains_completed_checkpoint_and_stable_id(self):
        code = """import os,sys
from run_state import RunState
s=RunState(sys.argv[1], {'source':'v1'})
s.step('agent:1', lambda: {'status':'completed'})
os._exit(9)
"""
        p = subprocess.run([sys.executable, '-c', code, str(self.run)],
                           env={**os.environ, 'PYTHONPATH': str(Path(__file__).parent)})
        self.assertEqual(p.returncode, 9)
        with self.state(resume=True) as state:
            rid = state.data['run_id']
            self.assertEqual(state.step('agent:1', lambda: self.fail('duplicate call')), {'status': 'completed'})
            events = [json.loads(row[0]) for row in state.db.execute('SELECT body FROM events ORDER BY seq')]
            self.assertEqual([e['event_name'] for e in events], ['run.started', 'step.started', 'step.checkpointed', 'run.resumed'])
            self.assertTrue(all(e['run_id'] == rid for e in events))
            self.assertEqual(events[1]['attempt_id'], rid + ':1')
            self.assertEqual(events[2]['outcome'], 'RUNNING')  # checkpoint durability is not gate PASS
            self.assertTrue(all(e['schema_version'] == 1 and e['event_id'] for e in events))

    def test_real_process_exit_inflight_is_never_automatically_retried(self):
        code = """import os,sys
from run_state import RunState
s=RunState(sys.argv[1], {'source':'v1'})
s.step('agent:1', lambda: os._exit(9))
"""
        subprocess.run([sys.executable, '-c', code, str(self.run)],
                       env={**os.environ, 'PYTHONPATH': str(Path(__file__).parent)}, check=False)
        with self.error('STATE_INFLIGHT_UNCERTAIN'):
            self.state(resume=True)
        with closing(sqlite3.connect(self.run / 'state.sqlite3')) as db:
            data = json.loads(db.execute('SELECT body FROM state').fetchone()[0])
            self.assertEqual(data['inflight'], 'agent:1')

    def test_duplicate_active_and_non_resume_runs_are_refused(self):
        with self.state():
            with self.error('STATE_WRITER_CONFLICT'):
                self.state(resume=True)
        with self.error('STATE_USAGE_INVALID'):
            self.state()

    def test_configuration_drift_and_artifact_tamper_are_rejected(self):
        with self.state() as state:
            state.step('gate:0', lambda: {'ok': False})
        with self.error('STATE_BINDING_MISMATCH'):
            self.state(resume=True, source='v2')
        (self.run / 'checkpoints/gate-0.json').write_text('{"ok": true}')
        with self.error('STATE_EVIDENCE_MISMATCH'):
            self.state(resume=True)

    def test_workspace_and_lessons_are_preserved_and_tamper_rejected(self):
        def work():
            (self.run / 'work').mkdir()
            (self.run / 'work/app.py').write_text('fixed')
            (self.run / 'lessons.md').write_text('lesson one')
            return {'ok': True}
        with self.state() as state:
            state.step('intake:0', work)
        with self.state(resume=True) as state:
            state.step('intake:0', lambda: self.fail('intake replayed'))
        self.assertEqual((self.run / 'work/app.py').read_text(), 'fixed')
        self.assertEqual((self.run / 'lessons.md').read_text(), 'lesson one')
        (self.run / 'lessons.md').write_text('changed')
        with self.error('STATE_EVIDENCE_MISMATCH'):
            self.state(resume=True)

    def test_completed_loop_resume_recreates_projection_without_any_call(self):
        with self.gate_result({'ok': True, 'release_eligible': True, 'status': 'PASS'}), \
                patch.object(loop, 'agent') as agent:
            self.assertEqual(self.cli(), 0)
            agent.assert_not_called()
        original = (self.run / 'evidence.json').read_bytes()
        (self.run / 'evidence.json').unlink()
        with patch.object(loop, 'gate', side_effect=AssertionError('gate repeated')), \
                patch.object(loop, 'run_json', side_effect=AssertionError('intake repeated')):
            self.assertEqual(self.cli(True), 0)
        self.assertEqual((self.run / 'evidence.json').read_bytes(), original)
        (self.upload / 'app.py').write_text('source changed')
        self.assertEqual(self.cli(True), 1)

    def test_resume_after_agent_checkpoint_skips_agent_and_baseline(self):
        fail = {'ok': False, 'status': 'FAIL', 'failure': {'class': 'F1', 'layer': 'L1', 'signature': 'missing'}}
        original_step = RunState.step
        def crash_after_agent(state, name, function, **kwargs):
            result = original_step(state, name, function, **kwargs)
            if name == 'agent:1':
                raise KeyboardInterrupt('crash after durable result')
            return result
        def proposal(*args):
            (self.run / 'work/Dockerfile').write_text('FROM scratch\n')
            (self.run / 'work/.jasmin').mkdir()
            (self.run / 'work/.jasmin/jasmin.yaml').write_text('app: sample\n')
            return 0, {'output': {'status': 'proposed'}, 'written': ['Dockerfile'], 'meta': {'duration_ms': 1, 'sdk_status': 'completed'}}
        with self.gate_result(fail), self.agent_result(side_effect=proposal), \
                patch.object(RunState, 'step', crash_after_agent):
            with self.assertRaises(KeyboardInterrupt):
                self.cli()
        with patch.object(loop, 'agent', side_effect=AssertionError('model called again')), \
                self.gate_result({'ok': True, 'release_eligible': True, 'status': 'PASS'}) as gate:
            self.assertEqual(self.cli(True), 0)
            self.assertEqual(gate.call_count, 1)
            self.assertEqual(gate.call_args.args[2], 1)
        ev = json.loads((self.run / 'evidence.json').read_text())
        self.assertIsNone(ev['llm_calls'])
        self.assertEqual(ev['agent_attempts'], 1)
        self.assertEqual(ev['sdk_invocations'], 1)
        self.assertEqual([a['attempt'] for a in ev['attempts']], [0, 1])
        self.assertEqual(ev['attempts'][1]['role'], 'adapter')
        self.assertEqual(ev['attempts'][1]['repair_scope'], 'packaging')

    def test_binding_tracks_gate_and_schema_but_not_test_files(self):
        from types import SimpleNamespace
        fixture = self.root / 'platform'
        for directory in ('gate', 'schemas'):
            (fixture / directory).mkdir(parents=True)
        gate_source = fixture / 'gate/gate.py'
        gate_source.write_text('v1')
        schema = fixture / 'schemas/app.schema.json'
        schema.write_text('{}')
        (fixture / 'observability.py').write_text('v1')
        (fixture / 'process.py').write_text('v1')
        for name in ('execution.py','storage.py','infra/database.py'):
            path=fixture/name; path.parent.mkdir(exist_ok=True); path.write_text('v1')
        args = SimpleNamespace(upload=str(self.upload), run=str(self.run), request=None, resume=False, self_test=False)
        with patch.object(loop, 'PLATFORM', fixture):
            first = loop.binding(args)
            (fixture / 'gate/test_gate.py').write_text('test only')
            self.assertEqual(loop.binding(args), first)
            gate_source.write_text('v2')
            self.assertNotEqual(loop.binding(args), first)
            gate_source.write_text('v1')
            schema.write_text('{"new":"policy"}')
            self.assertNotEqual(loop.binding(args), first)
            schema.write_text('{}')
            (fixture / 'observability.py').write_text('v2')
            self.assertNotEqual(loop.binding(args), first)
            (fixture / 'observability.py').write_text('v1')
            for name in ('execution.py','storage.py','infra/database.py'):
                path=fixture/name; path.write_text('v2')
                self.assertNotEqual(loop.binding(args), first)
                path.write_text('v1')

    def test_uncertain_agent_failure_blocks_resume_and_partial_cannot_pass(self):
        fail = {'ok': False, 'failure': {'class': 'F1', 'layer': 'L1', 'signature': 'missing'}}
        error = StateError('SDK_OUTCOME_UNKNOWN', component='loop', phase='agent', outcome='UNKNOWN',
                           retry_policy='after_reconcile', side_effect='unknown')
        with self.gate_result(fail), patch.object(loop, 'agent', side_effect=error):
            self.assertEqual(self.cli(), 1)
        with patch.object(loop, 'agent', side_effect=AssertionError('retried')):
            self.assertEqual(self.cli(True), 1)

    def test_partial_gate_claim_cannot_become_release(self):
        with self.gate_result({'ok': True, 'release_eligible': True}):
            self.assertEqual(self.cli(False, '--layers', 'L0,L1'), 1)
        self.assertFalse(json.loads((self.run / 'evidence.json').read_text())['passed'])

    def test_provider_unknown_receipt_is_blocked_even_with_an_output(self):
        fail = {'ok': False, 'failure': {'class': 'F1', 'layer': 'L1', 'signature': 'missing'}}
        rec = {'meta': {'status': 'unknown', 'session_id': 'synthetic-session'}, 'output': {'status': 'proposed'}}
        with self.gate_result(fail) as gate, patch.object(loop, 'agent', return_value=(0, rec)):
            self.assertEqual(self.cli(), 1)
            self.assertEqual(gate.call_count, 1)
        with patch.object(loop, 'agent', side_effect=AssertionError('unknown call retried')):
            self.assertEqual(self.cli(True), 1)

    def test_native_artifacts_and_git_baseline_are_bound(self):
        with self.state() as state:
            (self.run / 'work/.git').mkdir(parents=True)
            (self.run / 'work/.git/HEAD').write_text('trusted baseline')
            (self.run / 'native.json').write_text('{}')
            state.step('gate:0', lambda: {'ok': False}, artifacts=('native.json',))
            with self.assertRaises(sqlite3.IntegrityError):
                state.db.execute('DELETE FROM events')
        (self.run / 'native.json').write_text('{"ok":true}')
        with self.error('STATE_EVIDENCE_MISMATCH'):
            self.state(resume=True)
        (self.run / 'native.json').write_text('{}')
        (self.run / 'work/.git/HEAD').write_text('modified baseline')
        with self.error('STATE_EVIDENCE_MISMATCH'):
            self.state(resume=True)

    def test_intake_overlap_and_secret_filename_guard_preserve_original(self):
        script = loop.PLATFORM / 'poc/intake.py'
        for work, run in [(self.upload, self.root / 'out'), (self.root, self.root / 'out')]:
            p = subprocess.run([sys.executable, str(script), str(self.upload), str(work), str(run)], capture_output=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual((self.upload / 'app.py').read_text(), 'original\n')
        (self.upload / '.envrc').write_text('harmless sentinel')
        p = subprocess.run([sys.executable, str(script), str(self.upload), str(self.root / 'work'), str(self.run)], capture_output=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.root / 'work').exists())

    def test_timeout_start_and_invalid_output_have_distinct_safe_errors(self):
        secret = 'SYNTHETIC_DO_NOT_LOG'
        cases = [(subprocess.TimeoutExpired(['tool', secret], 1, output=secret), 'STEP_TIMEOUT', 'UNKNOWN', 'unknown'),
                 (FileNotFoundError(errno.ENOENT, secret), 'STEP_START_FAILED', 'BLOCKED', 'none')]
        for cause, code, outcome, effect in cases:
            with patch.object(loop, 'run_bounded', side_effect=cause), self.assertRaises(StateError) as caught:
                loop.run_json(['tool', secret], phase='agent')
            error = caught.exception
            self.assertEqual((error.code, error.outcome, error.side_effect), (code, outcome, effect))
            self.assertIs(error.__cause__, cause)
            self.assertTrue(error.as_dict()['causes'])
            self.assertNotIn(secret, json.dumps(error.as_dict()))
        with patch.object(loop, 'run_bounded', return_value=subprocess.CompletedProcess([], 0, secret, secret)), \
                self.assertRaises(StateError) as caught:
            loop.run_json(['tool'], phase='gate')
        self.assertEqual(caught.exception.code, 'STEP_OUTPUT_INVALID')
        self.assertNotIn(secret, json.dumps(caught.exception.as_dict()))

    def test_definite_start_failure_is_safe_to_resume_but_is_not_checkpointed(self):
        error = StateError('STEP_START_FAILED', component='loop', phase='gate', retry_policy='safe')
        with self.state() as state:
            with self.error('STEP_START_FAILED'):
                state.step('gate:0', lambda: (_ for _ in ()).throw(error))
            self.assertIsNone(state.data['inflight'])
            self.assertNotIn('gate:0', state.data['steps'])
        with self.state(resume=True) as state:
            state.step('gate:0', lambda: {'status': 'FAIL'})
            rows = [json.loads(r[0]) for r in state.db.execute('SELECT body FROM events ORDER BY seq')]
            blocked = next(e for e in rows if e['event_name'] == 'step.interrupted')
            self.assertEqual(blocked['outcome'], 'BLOCKED')
            self.assertEqual(blocked['error']['code'], 'STEP_START_FAILED')

    def test_sql_commit_and_final_artifact_failures_preserve_typed_causes(self):
        secret = 'SYNTHETIC_STORAGE_SECRET'
        with self.state() as state:
            fake = MagicMock()
            cause = sqlite3.OperationalError(secret)
            fake.execute.side_effect = cause
            with patch.object(state, 'db', fake), self.assertRaises(StateError) as caught:
                state.save('run.test')
            self.assertEqual(caught.exception.code, 'STATE_STORAGE_FAILED')
            self.assertEqual(caught.exception.outcome, 'UNKNOWN')
            self.assertIs(caught.exception.__cause__, cause)
            self.assertNotIn(secret, json.dumps(caught.exception.as_dict()))
            disk = OSError(errno.ENOSPC, secret)
            with patch.object(run_state, 'atomic_json', side_effect=disk), self.assertRaises(StateError) as caught:
                state.complete({'passed': True})
            self.assertEqual(caught.exception.code, 'STATE_STORAGE_FAILED')
            self.assertEqual(caught.exception.as_dict()['causes'][0]['errno'], errno.ENOSPC)
            self.assertNotIn(secret, json.dumps(caught.exception.as_dict()))

    def test_attempt_sidecar_references_match_renamed_files(self):
        self.run.mkdir()
        def child(*args, **kwargs):
            record = {'meta': {'events_file': 'adapter-events.jsonl', 'session_file': 'adapter-session.json'},
                      'output': {'status': 'proposed'}}
            (self.run / 'adapter.json').write_text(json.dumps(record))
            (self.run / 'adapter-events.jsonl').write_text('{}\n')
            (self.run / 'adapter-session.json').write_text('{}')
            return 0, {}, ''
        with patch.object(loop, 'run_json', side_effect=child):
            rc, rec = loop.agent('adapter', 'codex', self.upload, self.run, 1, 3, None)
        saved = json.loads((self.run / 'adapter-1.json').read_text())
        self.assertEqual(saved, rec)
        for key in ('events_file', 'session_file'):
            self.assertTrue((self.run / rec['meta'][key]).is_file())
            self.assertTrue(rec['meta'][key].startswith('adapter-1-'))

    def test_known_sdk_failure_preserves_structured_error_in_cli_and_evidence(self):
        fail = {'ok': False, 'failure': {'class': 'F1', 'layer': 'L1', 'signature': 'missing'}}
        detail = StateError('SDK_POLICY_DENIED', component='runner', phase='policy').as_dict()
        rec = {'meta': {'status': 'failed', 'sdk_status': 'not_started'}, 'output': {}, 'error': detail}
        stream = io.StringIO()
        with self.gate_result(fail), self.agent_result(rec, rc=1), redirect_stdout(stream):
            self.assertEqual(self.cli(), 1)
        printed = json.loads(stream.getvalue().splitlines()[-1])
        saved = json.loads((self.run / 'evidence.json').read_text())
        self.assertEqual(printed['status'], 'BLOCKED')
        self.assertEqual(printed['error'], detail)
        self.assertEqual(saved['error'], detail)
        self.assertEqual((saved['agent_attempts'], saved['sdk_invocations'], saved['llm_calls']), (1, 0, 0))
        self.assertEqual(saved['cost_status'], 'no_calls')

    def test_missing_sdk_receipt_keeps_safe_stdout_error_and_blocks_unknown(self):
        self.run.mkdir()
        cause = OSError(errno.ENOSPC, 'SYNTHETIC_SECRET_MUST_NOT_APPEAR')
        detail = StateError('OBSERVATION_WRITE_FAILED', component='runner', phase='observation',
                            outcome='UNKNOWN', retry_policy='after_reconcile', side_effect='unknown', cause=cause).as_dict()
        with patch.object(loop, 'run_json', return_value=(1, {'error': detail}, '')):
            rc, record = loop.agent('adapter', 'codex', self.upload, self.run, 1, 3, None)
        self.assertEqual(rc, 1)
        self.assertEqual(record['error'], detail)
        self.assertEqual(record['meta']['receipt_source'], 'stdout_event')
        self.assertNotIn('SYNTHETIC_SECRET_MUST_NOT_APPEAR', json.dumps(record))
        # The process boundary preserves the upstream code/cause rather than replacing it with missing-file noise.
        local_run = self.root / 'resume-run'
        with RunState(local_run, {'source': 'v1'}) as state:
            args = type('Args', (), {'provider': 'codex', 'repair_scope': 'packaging', 'max_attempts': 1,
                    'upload': str(self.upload), 'quality_network': None,
                    'selected_root': None, 'layers': 'L0,L1,Q,L2,L4,L3', 'request': None})()
            fail = {'ok': False, 'failure': {'class': 'F1', 'layer': 'L1', 'signature': 'missing'}}
            (local_run / 'ir.json').write_text('{}')
            with patch.object(loop, 'run_json', return_value=(0, {'ok': True}, '')), \
                    self.gate_result(fail), patch.object(loop, 'agent', return_value=(1, record)), \
                    self.assertRaises(StateError) as caught:
                loop.execute(args, local_run, state)
            self.assertEqual(caught.exception.as_dict(), detail)
            self.assertEqual(state.data['inflight'], 'agent:1')

    def test_gate_stdout_unknown_cannot_be_promoted_by_stale_pass_receipt(self):
        gate_dir = self.run / 'gate-0'
        gate_dir.mkdir(parents=True)
        (gate_dir / 'verdict.json').write_text(json.dumps({'ok': True, 'status': 'PASS', 'release_eligible': True, 'error': None}))
        detail = StateError('OBSERVATION_WRITE_FAILED', component='gate', phase='evidence-write', outcome='UNKNOWN',
                            retry_policy='after_reconcile', side_effect='possible').as_dict()
        with patch.object(loop, 'run_json', return_value=(1, {'ok': False, 'status': 'UNKNOWN', 'error': detail}, '')), \
                self.assertRaises(StateError) as caught:
            loop.gate(self.upload, self.run, 0, 'L0,L1,Q,L2,L4,L3')
        self.assertEqual(caught.exception.as_dict(), detail)
        with patch.object(loop, 'run_json', return_value=(1, {'ok': True, 'status': 'PASS', 'error': None}, '')), \
                self.error('STEP_OUTPUT_INVALID'):
            loop.gate(self.upload, self.run, 0, 'L0,L1,Q,L2,L4,L3')

    def test_finish_rejects_error_outcome_contradiction(self):
        detail = StateError('SDK_OUTCOME_UNKNOWN', component='runner', phase='invoke', outcome='UNKNOWN').as_dict()
        with self.error('STATE_EVIDENCE_MISMATCH'):
            loop.finish(self.run, {'status': 'FAIL', 'error': detail}, 0)

    def test_real_intake_policy_rejection_is_fail_not_execution_unknown(self):
        (self.upload / '.envrc').write_text('harmless sentinel')
        stream = io.StringIO()
        with patch.object(loop, 'gate', side_effect=AssertionError('gate must not run')), redirect_stdout(stream):
            self.assertEqual(self.cli(), 1)
        result = json.loads(stream.getvalue().splitlines()[-1])
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['error']['code'], 'INTAKE_REJECTED')
        self.assertEqual(result['error']['side_effect'], 'none')

    def test_timeout_kills_real_descendant_even_when_it_ignores_term(self):
        marker = self.root / 'escaped-completion'
        child = self.root / 'child.py'
        child.write_text("import signal,time,pathlib\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                         f"time.sleep(1)\npathlib.Path({str(marker)!r}).write_text('late')\n")
        parent = self.root / 'parent.py'
        parent.write_text("import subprocess,sys,time\n"
                          f"subprocess.Popen([sys.executable,{str(child)!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                          "time.sleep(5)\n")
        bounded = loop.run_bounded
        with patch.object(loop, 'run_bounded', side_effect=lambda cmd, **kw: bounded(cmd, timeout=0.5)), \
                self.assertRaises(StateError) as caught:
            loop.run_json([sys.executable, str(parent)], phase='agent')
        self.assertEqual((caught.exception.code, caught.exception.outcome), ('STEP_TIMEOUT', 'UNKNOWN'))
        time.sleep(1.1)
        self.assertFalse(marker.exists(), 'descendant continued after timeout')

    def test_output_limit_is_unknown_and_never_safe_to_retry(self):
        with patch.object(loop, 'run_bounded', side_effect=loop.OutputLimitError(128)), self.assertRaises(StateError) as caught:
            loop.run_json(['synthetic'], phase='agent')
        self.assertEqual((caught.exception.code, caught.exception.outcome, caught.exception.retry_policy),
                         ('STEP_OUTPUT_INVALID', 'UNKNOWN', 'after_reconcile'))

    def test_final_runner_event_error_overrides_completed_disk_receipt(self):
        sys.path.insert(0, str(loop.PLATFORM / 'runner'))
        import run_agent
        self.run.mkdir()
        real_open = Path.open
        observed = {}
        def provider(cfg, system, task, schema, workspace, run, deny, emit):
            emit('session.finished', sdk_status='completed', session_id='offline-fixture')
            return {'status':'proposed', 'summary':'offline', 'files_changed':[],
                    'assumptions':[], 'confidence':'high', 'files':[]}, {}
        def open_fault(path, mode='r', *args, **kwargs):
            if path == self.run/'fixer-events.jsonl' and mode == 'a' and (self.run/'fixer.json').exists():
                raise OSError(errno.ENOSPC, 'private sentinel')
            return real_open(path, mode, *args, **kwargs)
        def child(*args, **kwargs):
            argv = ['runner', 'fixer', '--workspace', str(self.upload), '--run', str(self.run),
                    '--task', str(self.run/'task-1.md')]
            stream = io.StringIO()
            with patch.object(sys, 'argv', argv), patch.object(run_agent, 'run_codex', side_effect=provider), \
                    patch.object(Path, 'open', open_fault), redirect_stdout(stream):
                rc = run_agent.main()
            out = json.loads(stream.getvalue().splitlines()[-1]); observed.update(out)
            return rc, out, ''
        with patch.object(loop, 'run_json', side_effect=child):
            rc, receipt = loop.agent('fixer', 'codex', self.upload, self.run, 1, 3, None)
        self.assertEqual(rc, 1)
        self.assertEqual(receipt['error'], observed['error'])
        self.assertEqual(receipt['error']['code'], 'OBSERVATION_WRITE_FAILED')
        self.assertEqual(receipt['meta']['status'], 'failed')
        self.assertEqual(receipt['meta']['sdk_status'], 'completed')
        self.assertNotIn('private sentinel', json.dumps(receipt))

    def test_missing_required_artifact_blocks_checkpoint_and_resume(self):
        with self.state() as state:
            with self.error('STATE_STORAGE_FAILED'):
                state.step('agent:1', lambda: {'ok': True}, artifacts=('required.json',))
            self.assertNotIn('agent:1', state.data['steps'])
            self.assertEqual(state.data['inflight'], 'agent:1')
        with self.error('STATE_INFLIGHT_UNCERTAIN'):
            self.state(resume=True)

    def test_optional_artifact_can_be_absent_and_final_error_is_in_event(self):
        detail = StateError('SDK_CONFIG_INVALID', component='runner', phase='config',
                            retry_policy='after_configuration').as_dict()
        with self.state() as state:
            state.step('gate:0', lambda: {'ok': False}, optional_artifacts=('failure.txt',))
            state.complete({'passed': False, 'status': 'BLOCKED', 'error': detail})
            event = json.loads(state.db.execute('SELECT body FROM events ORDER BY seq DESC LIMIT 1').fetchone()[0])
            self.assertEqual(event['error'], detail)
            self.assertEqual(event['outcome'], 'BLOCKED')

    def test_existing_run_root_is_private_before_outputs_and_symlink_is_refused(self):
        self.run.mkdir(mode=0o755); self.run.chmod(0o755)
        with self.state():
            self.assertEqual(self.run.stat().st_mode & 0o777, 0o700)
        linked = self.root/'linked-run'; linked.symlink_to(self.run, target_is_directory=True)
        with self.assertRaises(StateError):
            RunState(linked, {'source': 'v1'}, resume=True)

    def test_effective_codex_home_drift_is_bound_without_reading_credentials(self):
        from types import SimpleNamespace
        args = SimpleNamespace(upload=str(self.upload), run=str(self.run), request=None,
                               resume=False, self_test=False, provider='codex')
        with patch.dict(os.environ, {'RAILSHOT_AUTH_MODE':'subscription', 'RAILSHOT_CODEX_HOME':''}):
            with patch.dict(os.environ, {'CODEX_HOME':'/offline/account-A'}): first = loop.binding(args)
            with patch.dict(os.environ, {'CODEX_HOME':'/offline/account-B'}): second = loop.binding(args)
        self.assertNotEqual(first['auth_route_sha256'], second['auth_route_sha256'])

    def test_honest_attempt_sdk_and_model_counts_distinguish_pre_call_failure(self):
        self.run.mkdir()
        for status, sdk_count, llm_count, cost_status in [('not_started', 0, 0, 'no_calls'),
                                                       ('completed', 1, None, 'unknown'),
                                                       ('unknown', None, None, 'unknown')]:
            with self.subTest(status=status), redirect_stdout(io.StringIO()):
                evidence = {'attempts':[{'agent_invoked':True, 'agent_meta':{'sdk_status':status}}],
                            'result':'stop: fixture'}
                loop.finish(self.run, evidence, time.time())
            self.assertEqual(evidence['agent_attempts'], 1)
            self.assertEqual(evidence['sdk_invocations'], sdk_count)
            self.assertEqual(evidence['llm_calls'], llm_count)
            self.assertEqual(evidence['cost_status'], cost_status)

    def test_selected_root_is_forwarded_to_gate_and_changes_resume_binding(self):
        from types import SimpleNamespace
        target = self.run/'gate-0'; target.mkdir(parents=True)
        verdict = {'ok': False, 'status':'BLOCKED', 'error':None}
        (target/'verdict.json').write_text(json.dumps(verdict))
        with patch.object(loop, 'run_json', return_value=(1, verdict, '')) as child:
            loop.gate(self.upload, self.run, 0, 'L0', selected_root='apps/api')
        command = child.call_args.args[0]
        self.assertEqual(command[command.index('--selected-root') + 1], 'apps/api')
        args = SimpleNamespace(upload=str(self.upload), run=str(self.run), request=None,
                               resume=False, self_test=False, provider='codex', selected_root='apps/api')
        first = loop.binding(args)
        args.selected_root = 'apps/web'
        self.assertNotEqual(first, loop.binding(args))


if __name__ == '__main__':
    unittest.main()
