#!/usr/bin/env python3
"""Intake → deterministic baseline → optional adapter/packaging fixer → evidence.

usage:
  loop.py UPLOAD_DIR RUN_DIR [--provider claude|codex] [--max-attempts 3] [--layers L0,...] [--request FILE]
  loop.py --self-test

Stops on: gate pass, give_up, class F7/F8/INJ, the same failure signature twice, or N attempts.
The LLM never decides pass/fail; only gate verdicts do.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_state import RunState, StateError, atomic_json, digest, tree_digest
from process import OutputLimitError, run_bounded
from execution import GATE_ORDER
from runner.runtime_boundary import effective_auth_route

PLATFORM = Path(__file__).resolve().parents[1]
PY = [sys.executable]
FIXABLE = {"F1", "F2", "F4", "F5", "F6", "F9"}
STOP = {"F7": "application code defect", "F8": "transient infrastructure failure", "INJ": "suspected prompt injection",
        "QUALITY": "quality failure requires reviewed application changes; automatic source editing is disabled"}


def run_json(cmd, cwd=None, *, phase='subprocess'):
    try:
        p = run_bounded(cmd, cwd=cwd, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise StateError('STEP_TIMEOUT', component='loop', phase=phase, outcome='UNKNOWN',
                         retry_policy='after_reconcile', side_effect='unknown', cause=exc) from exc
    except (FileNotFoundError, PermissionError) as exc:
        raise StateError('STEP_START_FAILED', component='loop', phase=phase,
                         retry_policy='safe', side_effect='none', cause=exc) from exc
    except (OSError, OutputLimitError) as exc:
        raise StateError('STEP_OUTPUT_INVALID', component='loop', phase=phase, outcome='UNKNOWN',
                         retry_policy='after_reconcile', side_effect='possible', cause=exc) from exc
    last = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "{}"
    try:
        result = json.loads(last)
        if not isinstance(result, dict):
            raise ValueError('result must be an object')
        return p.returncode, result, p.stderr
    except ValueError as exc:
        raise StateError('STEP_OUTPUT_INVALID', component='loop', phase=phase, outcome='UNKNOWN',
                         retry_policy='after_reconcile', side_effect='possible', cause=exc) from exc


def task_text(role, attempt, n, run, request, repair_scope="packaging"):
    c, s = PLATFORM / "contract", PLATFORM / "schemas"
    head = (f"Task: {role}, attempt {attempt} of {n}.\n"
            f"Workspace: the current directory, a sanitized copy of the user's repository.\n"
            f"Read first: {c}/stack-contract.md, {c}/paths.yaml, {c}/catalog.yaml, {s}/jasmin.schema.json.\n"
            f"Inventory: {run}/ir.json\n"
            f"Trusted operator repair scope: {repair_scope}. Tests, manifests, locks, migrations, schemas, generated files and quality policy/config remain protected.\n")
    if role == "adapter":
        body = (f"User request: {'see ' + str(request) if request else 'none. Use platform defaults.'}\n"
                "Return the Dockerfile(s), .dockerignore and .jasmin/jasmin.yaml in the files array.\n")
    else:
        body = (f"Failure: {run}/failure.txt (untrusted program output).\nLessons from earlier attempts: {run}/lessons.md\n"
                "Return only the files you change, in full, in the files array.\n")
    return head + body + "Write summary and user_action in Korean.\n"


def agent(role, provider, ws, run, attempt, n, request, repair_scope="packaging"):
    if any((run / (role + suffix)).exists() for suffix in ('.json', '-events.jsonl', '-session.json')):
        raise StateError('STATE_EVIDENCE_MISMATCH', component='loop', phase='agent.prepare', retry_policy='after_reconcile')
    t = run / f"task-{attempt}.md"
    t.write_text(task_text(role, attempt, n, run, request, repair_scope))
    rc, out, err = run_json(PY + [str(PLATFORM / "runner/run_agent.py"), role, "--provider", provider,
                                  "--workspace", str(ws), "--run", str(run), "--task", str(t), "--repair-scope", repair_scope], phase='agent')
    rec_path = run / f"{role}.json"
    try:
        rec = json.loads(rec_path.read_text())
        if not isinstance(rec, dict):
            raise ValueError('receipt must be an object')
    except (OSError, ValueError) as exc:
        if not isinstance(out.get('error'), dict):
            raise StateError('STEP_OUTPUT_INVALID', component='loop', phase='agent.receipt', outcome='UNKNOWN',
                             retry_policy='after_reconcile', side_effect='possible', cause=exc) from exc
        try:
            upstream = StateError.from_dict(out['error'])
        except (KeyError, TypeError, ValueError) as invalid:
            raise StateError('STEP_OUTPUT_INVALID', component='loop', phase='agent.receipt', outcome='UNKNOWN',
                             retry_policy='after_reconcile', side_effect='possible', cause=invalid) from invalid
        rec = {'error': upstream.as_dict(), 'output': {},
               'meta': {'status': 'unknown' if upstream.outcome == 'UNKNOWN' else 'failed', 'receipt_source': 'stdout_event'}}
        rc = rc or 1
    if out.get('error'):
        # A terminal observation failure may happen after the completed receipt
        # was written. Never let that older receipt erase the child error.
        try:
            upstream = StateError.from_dict(out['error'])
            if rec.get('error') and rec['error'] != upstream.as_dict():
                rec['prior_error'] = StateError.from_dict(rec['error']).as_dict()
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError('STEP_OUTPUT_INVALID', component='loop', phase='agent.receipt', outcome='UNKNOWN',
                             retry_policy='after_reconcile', side_effect='possible', cause=exc) from exc
        rec['error'] = upstream.as_dict()
        rec.setdefault('meta', {}).update(status='unknown' if upstream.outcome == 'UNKNOWN' else 'failed',
                                         receipt_source='stdout_event')
        rec['meta'].setdefault('sdk_status', 'not_started' if upstream.side_effect == 'none' else 'unknown')
        rc = rc or 1
    elif rc and not rec.get('error'):
        raise StateError('STEP_OUTPUT_INVALID', component='loop', phase='agent.receipt', outcome='UNKNOWN',
                         retry_policy='after_reconcile', side_effect='possible')
    for suffix in ('events.jsonl', 'session.json'):
        p = run / f'{role}-{suffix}'
        if p.exists():
            target = f'{role}-{attempt}-{suffix}'
            p.rename(run / target)
            rec.setdefault('meta', {})['events_file' if suffix == 'events.jsonl' else 'session_file'] = target
    atomic_json(run / f'{role}-{attempt}.json', rec)
    rec_path.unlink(missing_ok=True)
    return rc, rec


def gate(ws, run, attempt, layers, *, quality_network=None, repair_scope="packaging", selected_root=None):
    g = run / f"gate-{attempt}"
    flags = ["--quality-network", quality_network] if quality_network else []
    flags += ["--repair-scope", repair_scope]
    if selected_root is not None:
        flags += ["--selected-root", selected_root]
    rc, out, err = run_json(PY + [str(PLATFORM / "gate/gate.py"), str(ws), str(g), "--layers", layers, *flags], phase='gate')
    if out.get('status') == 'UNKNOWN':
        try:
            upstream = StateError.from_dict(out['error'])
        except (KeyError, ValueError) as exc:
            raise StateError('STEP_OUTPUT_INVALID', component='loop', phase='gate.output', outcome='UNKNOWN',
                             retry_policy='after_reconcile', side_effect='possible', cause=exc) from exc
        raise upstream
    try:
        verdict = json.loads((g / 'verdict.json').read_text())
    except (OSError, ValueError) as exc:
        raise StateError('STEP_OUTPUT_INVALID', component='loop', phase='gate.receipt', outcome='UNKNOWN',
                         retry_policy='after_reconcile', side_effect='possible', cause=exc) from exc
    if (not isinstance(verdict, dict) or (rc == 0) != (out.get('ok') is True)
            or out.get('ok') != verdict.get('ok') or out.get('status') != verdict.get('status')
            or out.get('error') != verdict.get('error')):
        raise StateError('STEP_OUTPUT_INVALID', component='loop', phase='gate.receipt', outcome='UNKNOWN',
                         retry_policy='after_reconcile', side_effect='possible')
    if (g / "failure.txt").exists():
        (run / "failure.txt").write_text((g / "failure.txt").read_text())
    return verdict


def decide(verdict, report, seen, repair_scope="packaging"):
    """Return (stop_reason | None). Pure function; see self-test."""
    if verdict.get("ok") and verdict.get("release_eligible"):
        return "passed"
    if verdict.get("checks_ok") or verdict.get("status") == "INCOMPLETE":
        return "incomplete: partial gates are diagnostic only"
    if any(l.get("blocked") for l in verdict.get("layers", [])):
        return "blocked: " + next(l["blocked"] for l in verdict["layers"] if l.get("blocked"))
    if report and report.get("status") == "give_up":
        return f"give_up: {report.get('give_up', {}).get('class')}"
    f = verdict.get("failure") or {}
    if f.get("class") == "QUALITY" and repair_scope == "source" and f.get("layer") == "Q" and f.get("source_repair_eligible") is True:
        return "stop: same failure twice" if f.get("signature") in seen else None
    if f.get("class") in STOP:
        return f"stop: {STOP[f['class']]}"
    if f.get("class") not in FIXABLE:
        return f"stop: unclassified failure {f.get('class')}"
    if f.get("signature") in seen:
        return "stop: same failure twice"
    return None


def binding(a):
    files = [p for folder in ('loop', 'poc', 'runner', 'gate', 'contract', 'schemas', 'agents')
             for p in (PLATFORM / folder).rglob('*') if p.is_file()
             and p.suffix in ('.py', '.yaml', '.json', '.md')
             and not any(x in p.parts for x in ('__pycache__', 'fixtures')) and not p.name.startswith('test_')]
    files += [PLATFORM / name for name in ('observability.py', 'process.py', 'execution.py', 'storage.py', 'infra/database.py')]
    return {**{k: v for k, v in vars(a).items() if k not in ('resume', 'self_test')},
            'upload': str(Path(a.upload).resolve()), 'source_sha256': tree_digest(Path(a.upload)),
            'request_sha256': digest(a.request) if a.request else None,
            'python_version': list(sys.version_info[:3]),
            'auth_route_sha256': hashlib.sha256(json.dumps(effective_auth_route(getattr(a, 'provider', 'codex')), sort_keys=True).encode()).hexdigest(),
            'harness_sha256': hashlib.sha256(json.dumps({str(p.relative_to(PLATFORM)): digest(p) for p in sorted(files)}, sort_keys=True).encode()).hexdigest()}


def execute(a, run, state):
    ws = run / 'work'
    if 'final' in state.data:
        return finish(run, json.loads((run / state.data['final']).read_text()), state.data['started'], finalized=True)
    ev = {'run_id': state.data['run_id'], 'provider': a.provider, 'repair_scope': a.repair_scope,
          'max_attempts': a.max_attempts, 'attempts': [], 'started': int(state.data['started'])}

    def intake_step():
        rc, result, err = run_json(PY + [str(PLATFORM / 'poc/intake.py'), a.upload, str(ws), str(run)], phase='intake')
        if rc == 0 and result.get('ok'):
            (run / 'lessons.md').write_text('')
            result['has_spec'] = (ws / '.jasmin/jasmin.yaml').is_file()
            return result
        if rc == 2 and result.get('ok') is False:
            error = StateError('INTAKE_REJECTED', component='loop', phase='intake', outcome='FAIL',
                               retry_policy='after_configuration')
            return {**result, 'status': 'FAIL', 'error': error.as_dict()}
        raise StateError('STEP_OUTPUT_INVALID', component='loop', phase='intake', outcome='UNKNOWN',
                         retry_policy='after_reconcile', side_effect='possible')

    ev['intake'] = state.step('intake:0', intake_step, artifacts=lambda result: ('ir.json',) if result.get('ok') else ())
    if not ev['intake'].get('ok'):
        ev['result'] = 'rejected at intake: ' + str(ev['intake'].get('reason', 'unknown'))
        ev['status'], ev['error'] = ev['intake']['status'], ev['intake']['error']
        return finish(run, ev, state.data['started'], state)
    options = {'quality_network': a.quality_network, 'repair_scope': a.repair_scope,
               'selected_root': a.selected_root}
    seen, role, current_failure = set(), 'deterministic', {}
    for attempt in range(a.max_attempts + 1):
        os.environ['RAILSHOT_RUN_ID'] = state.data['run_id']
        os.environ['RAILSHOT_ATTEMPT_ID'] = f"{state.data['run_id']}:{attempt}"
        rec, report, attempt_scope = {}, None, 'packaging'
        if attempt:
            attempt_scope = 'source' if (role == 'fixer' and a.repair_scope == 'source'
                             and current_failure.get('layer') == 'Q'
                             and current_failure.get('source_repair_eligible') is True) else 'packaging'

            def agent_step():
                rc, record = agent(role, a.provider, ws, run, attempt, a.max_attempts, a.request, attempt_scope)
                if record.get('error'):
                    try:
                        upstream = StateError.from_dict(record['error'])
                    except ValueError as exc:
                        raise StateError('STEP_OUTPUT_INVALID', component='loop', phase='agent.error', outcome='UNKNOWN',
                                         retry_policy='after_reconcile', side_effect='possible', cause=exc) from exc
                    record['error'], rc = upstream.as_dict(), rc or 1
                    if upstream.outcome == 'UNKNOWN':
                        raise upstream
                if ((record.get('meta') or {}).get('status') in ('unknown', 'pending', 'running')
                        or not isinstance(record.get('output'), dict)):
                    raise StateError('SDK_OUTCOME_UNKNOWN', component='loop', phase='agent', outcome='UNKNOWN',
                                     retry_policy='after_reconcile', side_effect='unknown')
                # The runner keeps its report; checkpoint only the control fields, never raw model text.
                output = record.get('output') or {}
                return {**{k: record.get(k) for k in ('meta', 'written', 'rejected', 'instructions_sha256', 'error')},
                        'role': role, 'repair_scope': attempt_scope,
                        'exit_code': rc, 'output': {k: output.get(k) for k in ('status', 'give_up')}}

            sidecars = (f'{role}-{attempt}-events.jsonl', f'{role}-{attempt}-session.json')
            rec = state.step(f'agent:{attempt}', agent_step,
                             artifacts=lambda result: (f'{role}-{attempt}.json',) + (sidecars if not result['exit_code'] else ()),
                             optional_artifacts=sidecars)
            if rec['role'] != role or rec['repair_scope'] != attempt_scope:
                raise StateError('STATE_EVIDENCE_MISMATCH', component='loop', phase='agent.checkpoint', retry_policy='after_reconcile')
            report = rec.get('output')
            if rec.get('exit_code'):
                ev['attempts'].append({'attempt': attempt, 'attempt_id': os.environ['RAILSHOT_ATTEMPT_ID'],
                                       'role': role, 'agent_invoked': True, 'agent_meta': rec.get('meta')})
                ev['result'] = 'stop: agent proposal rejected'
                ev['error'] = rec.get('error')
                ev['status'] = (rec.get('error') or {}).get('outcome', 'FAIL')
                return finish(run, ev, state.data['started'], state)
        verdict = state.step(f'gate:{attempt}', lambda: gate(ws, run, attempt, a.layers, **options),
                             artifacts=(f'gate-{attempt}/verdict.json',),
                             optional_artifacts=(f'gate-{attempt}/failure.txt', f'gate-{attempt}/progress.jsonl'))
        f = verdict.get('failure') or {}
        ev['attempts'].append({'attempt': attempt, 'attempt_id': os.environ['RAILSHOT_ATTEMPT_ID'], 'role': role,
            'repair_scope': attempt_scope, 'agent_invoked': bool(attempt), 'agent_meta': rec.get('meta'),
            'written': rec.get('written'), 'rejected': rec.get('rejected'),
            'instructions_sha256': rec.get('instructions_sha256'), 'report_status': report and report.get('status'),
            'verdict_ok': verdict.get('ok'), 'verdict_status': verdict.get('status'),
            'failure': f and {k: f.get(k) for k in ('layer', 'class', 'signature', 'source_repair_eligible')}})
        reason = decide(verdict, report, seen, a.repair_scope)
        if reason == 'passed' and a.layers != ','.join(GATE_ORDER):
            reason = 'incomplete: partial gates are diagnostic only'
            ev['status'] = 'INCOMPLETE'
        if reason:
            ev['result'] = reason
            ev['error'] = verdict.get('error')
            ev.setdefault('status', 'PASS' if reason == 'passed' else
                          'INCOMPLETE' if verdict.get('checks_ok') else
                          verdict['status'] if verdict.get('status') in ('FAIL', 'BLOCKED', 'UNKNOWN', 'NOT_RUN', 'INCOMPLETE') else
                          'BLOCKED' if any(layer.get('blocked') for layer in verdict.get('layers', [])) else 'FAIL')
            return finish(run, ev, state.data['started'], state)
        if f.get('signature'):
            seen.add(f['signature'])
        if attempt:
            def lesson_step():
                with (run / 'lessons.md').open('a') as fh:
                    fh.write(f"- attempt {attempt} ({role}): changed {rec.get('written')}; still failed at "
                             f"{f.get('layer')} {f.get('class')}: {f.get('signature')}\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                return {'recorded': True}
            state.step(f'lesson:{attempt}', lesson_step)
        role = 'fixer' if attempt or ev['intake'].get('has_spec') else 'adapter'
        current_failure = f
    ev['result'] = 'stop: attempt limit reached'
    return finish(run, ev, state.data['started'], state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("upload", nargs="?")
    ap.add_argument("run", nargs="?")
    ap.add_argument("--provider", choices=["codex", "claude"], default="codex")
    ap.add_argument("--max-attempts", type=int, choices=range(0, 4), default=3)
    ap.add_argument("--layers", default=','.join(GATE_ORDER))
    ap.add_argument("--quality-network")
    ap.add_argument("--selected-root", help="Trusted relative build root for repository discovery")
    ap.add_argument("--repair-scope", choices=["packaging", "source"], default="packaging")
    ap.add_argument("--request", default=None)
    ap.add_argument("--resume", action="store_true", help="resume durable completed checkpoints; never retry uncertain calls")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    try:
        if not a.upload or not a.run:
            raise StateError('STATE_USAGE_INVALID', component='loop', phase='config', retry_policy='after_configuration')
        run, upload = Path(a.run).resolve(), Path(a.upload).resolve()
        if run == upload or run in upload.parents or upload in run.parents or Path(a.run).is_symlink():
            raise StateError('STATE_USAGE_INVALID', component='loop', phase='config', retry_policy='after_configuration')
        with RunState(run, binding(a), a.resume) as state:
            return execute(a, run, state)
    except StateError as exc:
        error = exc
    except OSError as exc:
        error = StateError('STATE_STORAGE_FAILED', component='loop', phase='observation',
                           retry_policy='after_reconcile', side_effect='unknown', cause=exc)
    except Exception as exc:
        error = StateError('INTERNAL_ERROR', component='loop', phase='execution', outcome='UNKNOWN',
                           retry_policy='after_reconcile', side_effect='unknown', cause=exc)
    detail = error.as_dict()
    print(json.dumps({'result': detail['summary'], 'passed': False, 'status': error.outcome, 'error': detail}))
    return 1


def finish(run, ev, t0, state=None, finalized=False):
    if ev.get('error'):
        error = StateError.from_dict(ev['error'])
        if ev.get('status') != error.outcome:
            raise StateError('STATE_EVIDENCE_MISMATCH', component='loop', phase='complete', retry_policy='after_reconcile')
        ev['error'] = error.as_dict()
    if finalized:
        atomic_json(run / 'evidence.json', ev)
        print(json.dumps({k: ev[k] for k in ('result', 'passed', 'agent_attempts', 'sdk_invocations', 'llm_calls', 'cost_usd', 'cost_status', 'duration_s', 'status', 'error') if k in ev}))
        return 0 if ev['passed'] else 1
    ev["duration_s"] = round(time.time() - t0, 1)
    agents = [x.get('agent_meta') or {} for x in ev['attempts'] if x.get('agent_invoked')]
    ev['agent_attempts'] = len(agents)
    statuses = [meta.get('sdk_status') for meta in agents]
    ev['sdk_invocations'] = sum(status != 'not_started' for status in statuses) if all(
        status in ('not_started', 'running', 'completed', 'failed') for status in statuses) else None
    # SDK invocations can contain multiple model requests; no provider request
    # counter is available here. Only definite pre-call/no-call cases are zero.
    ev['llm_calls'] = 0 if ev['sdk_invocations'] == 0 else None
    costs = [meta.get('cost_usd') for meta in agents if meta.get('sdk_status') != 'not_started']
    ev["cost_usd"] = round(sum(costs), 4) if all(isinstance(c, (int, float)) for c in costs) else None
    ev["cost_status"] = "unknown" if ev["cost_usd"] is None else "reported" if costs else "no_calls"
    ev["passed"] = ev.get("result") == "passed"
    ev.setdefault('status', 'PASS' if ev['passed'] else 'FAIL')
    ev["evidence_sha256"] = hashlib.sha256(json.dumps(ev, sort_keys=True).encode()).hexdigest()[:12]
    if state:
        state.complete(ev)
    atomic_json(run / "evidence.json", ev)
    print(json.dumps({k: ev[k] for k in ("result", "passed", "agent_attempts", "sdk_invocations", "llm_calls", "cost_usd", "cost_status", "duration_s", "status", "error") if k in ev}, ensure_ascii=False))
    return 0 if ev["passed"] else 1


def self_test():
    ok = {"ok": True, "release_eligible": True}
    fail = lambda cls, sig="s1": {"ok": False, "layers": [{"layer": "L3"}], "failure": {"layer": "L3", "class": cls, "signature": sig}}
    assert decide(ok, None, set()) == "passed"
    assert decide(fail("F4"), {"status": "proposed"}, set()) is None
    assert decide(fail("F4"), {"status": "proposed"}, {"s1"}) == "stop: same failure twice"
    assert decide(fail("F7"), {"status": "proposed"}, set()).startswith("stop: application")
    assert decide(fail("F8"), None, set()).startswith("stop: transient")
    assert decide(fail("F4"), {"status": "give_up", "give_up": {"class": "F7"}}, set()) == "give_up: F7"
    assert decide({"ok": False, "layers": [{"layer": "L2", "blocked": "docker daemon unavailable"}]}, None, set()).startswith("blocked")
    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
