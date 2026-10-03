"""Bind a proposal to host-captured failure facts. Text is never repair authority."""
import hashlib
import json
from pathlib import Path
import re

from source_snapshot import capture, entries_digest
from diagnostics import bounded


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(run):
    path = Path(run) / 'diagnostics/case.json'
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('repair evidence invalid')
    return path.read_bytes()


CONTEXT_VERSION = 2
FAILURE_BYTES = 4000
LOG_BYTES = 2000
MAX_LOGS = 2
MAX_PATHS = 24
# File names are discovery hints, not inferred entrypoints or permission grants.
START_FILES = {'Dockerfile', 'package.json', 'pom.xml', 'build.gradle', 'build.gradle.kts',
               'requirements.txt', 'pyproject.toml', 'go.mod', 'railshot.yaml', 'jasmin.yaml'}


def read_log(run, item):
    if not re.fullmatch(r'process-[1-9][0-9]*\.log', item['path']):
        raise ValueError('repair evidence log invalid')
    path = Path(run) / 'diagnostics' / item['path']
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError('repair evidence log invalid')
    data = path.read_bytes()
    if sha(data) != item['sha256']:
        raise ValueError('repair evidence log changed')
    return data


def previous_attempts(run):
    """Carry bounded model hypotheses separately from host-recorded application facts."""
    if run is None:
        return []
    paths = [p for p in Path(run).iterdir() if re.fullmatch(r'(adapter|fixer)-[0-9]+\.json', p.name)]
    result = []
    for path in sorted(paths, key=lambda p: int(p.stem.rsplit('-', 1)[1]))[-3:]:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError('repair evidence history invalid')
        record = json.loads(path.read_bytes())
        output = record.get('output') or {}
        attempt = int(path.stem.rsplit('-', 1)[1])
        verdict_path = Path(run) / f'gate-{attempt}' / 'verdict.json'
        gate_result = None
        if verdict_path.exists():
            if verdict_path.is_symlink() or verdict_path.stat().st_size > 1024 * 1024:
                raise ValueError('repair evidence history invalid')
            verdict = json.loads(verdict_path.read_bytes())
            gate_result = {'status': verdict.get('status'),
                           'failure_layer': (verdict.get('failure') or {}).get('layer')}
        result.append({
            'attempt': attempt, 'host_gate_result': gate_result,
            'applied_files': record.get('written') or [],
            'proposal_rejection': record.get('proposal_rejection'),
            'model_hypothesis_unverified': bounded(output.get('root_cause') or '', 1000)[0],
            'model_assumptions_unverified': bounded(json.dumps(output.get('assumptions') or [], ensure_ascii=False), 1000)[0],
        })
    return result


def context(raw, run=None, role='fixer'):
    """Supply observed evidence first; leave source semantics to bounded investigation."""
    case = json.loads(raw)
    failure, files = case['failure'], case['source']['files']
    excerpt, omitted = bounded(failure['excerpt'], FAILURE_BYTES)
    binding = {'case_id': case['case_id'], 'case_sha256': sha(raw),
               'source_sha256': case['source']['tested_sha256'], 'policy_sha256': case['policy_sha256']}
    located = {row['path'] for row in failure['locations']}
    paths = sorted(files, key=lambda name: (name not in located,
        Path(name).name not in START_FILES and not Path(name).name.lower().startswith('readme'), name))[:MAX_PATHS]
    logs = [p for p in case['processes'] if p['layer'] == failure['layer'] and p.get('log')]
    logs.sort(key=lambda p: (p['outcome'] == 'PASS', -int(p['id'].split('-')[1])))
    selected = []
    if run is not None:
        for process in logs[:MAX_LOGS]:
            item = process['log']
            text, cut = bounded(read_log(run, item).decode(), LOG_BYTES)
            selected.append({'id': process['id'], 'command_kind': process['command_kind'],
                'outcome': process['outcome'], 'text': text, 'reference':
                {'kind': 'log', 'id': process['id'], 'sha256': item['sha256']},
                'omitted_bytes': cut + item['omitted_bytes']})
    return {
        'context_version': CONTEXT_VERSION,
        'task': 'prepare_container' if role == 'adapter' else 'repair_failed_gate',
        'failed_gate': failure['layer'],
        'evidence_binding': binding,
        'failure': {**failure, 'excerpt': excerpt,
                    'excerpt_omitted_bytes': failure['excerpt_omitted_bytes'] + omitted},
        'failure_reference': {'kind': 'log', 'id': 'failure', 'sha256': sha(failure['excerpt'].encode())},
        'source_candidates': [{'path': name, **files[name]} for name in paths],
        'source_paths_omitted': max(0, len(files) - len(paths)),
        'logs': selected, 'other_log_count': len(logs) - len(selected),
        'checks': [{k: v for k, v in row.items() if k != 'duration_ms'} for row in case['checks']],
        'previous_attempts': previous_attempts(run),
        'missing_evidence': case['missing_evidence'],
    }


def verify(run, workspace, output, gate_order, repair_scope, expected):
    if not output.get('files'):
        return None
    if expected is None or load(run) != expected:
        raise ValueError('repair evidence missing or changed')
    case = json.loads(expected)
    policy, source = case['policy'], case['source']
    if case['case_id'] != sha(json.dumps([case['native_run_id'], case['attempt_id'], source['tested_sha256']], separators=(',', ':')).encode()):
        raise ValueError('repair evidence identity differs')
    actual_policy = sha(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode())
    policy_path = Path(__file__).resolve().parents[1] / 'contract/paths.yaml'
    if (case['policy_sha256'] != actual_policy or policy['protected_policy_sha256'] != sha(policy_path.read_bytes())
            or policy['gate_order'] != list(gate_order)
            or repair_scope != policy['repair_scope'] and repair_scope != 'packaging'
            or source['tested_sha256'] != source['after_sha256']
            or entries_digest(capture(workspace)) != source['tested_sha256']
            or case['verification']['release_eligible'] or case['verification']['gate_outcome'] != 'FAIL'):
        raise ValueError('repair evidence source or policy differs')
    binding = {'case_id': case['case_id'], 'case_sha256': sha(expected),
               'source_sha256': source['tested_sha256'], 'policy_sha256': actual_policy}
    if output.get('evidence_binding') != binding or output.get('addresses_failure') != case['failure']['fingerprint']:
        raise ValueError('repair evidence binding differs')
    refs = output.get('evidence_refs')
    if not isinstance(refs, list) or not 1 <= len(refs) <= 12:
        raise ValueError('repair evidence references required')

    def file_ref(path, line, digest=None):
        entry = source['files'].get(path)
        if (not entry or type(line) is not int or not 1 <= line <= entry['lines']
                or digest is not None and digest != entry['sha256']):
            raise ValueError('repair evidence file reference invalid')
        target = Path(workspace) / path
        if target.is_symlink() or sha(target.read_bytes()) != entry['sha256']:
            raise ValueError('repair evidence file changed')

    logs = {p['id']: p['log'] for p in case['processes'] if p.get('log')}
    for ref in refs:
        if set(ref) == {'kind', 'path', 'line', 'sha256'} and ref['kind'] == 'source':
            file_ref(ref['path'], ref['line'], ref['sha256'])
        elif set(ref) == {'kind', 'id', 'sha256'} and ref['kind'] == 'log':
            if ref['id'] == 'failure':
                data = case['failure']['excerpt'].encode()
                if not data:
                    raise ValueError('repair evidence failure is empty')
            else:
                item = logs.get(ref['id'])
                if not item:
                    raise ValueError('repair evidence log invalid')
                data = read_log(run, item)
            if sha(data) != ref['sha256']:
                raise ValueError('repair evidence log hash differs')
        else:
            raise ValueError('repair evidence reference invalid')
    # Reject fabricated file:line citations even inside explanatory prose.
    text = '\n'.join([output.get('root_cause') or '', *(output.get('assumptions') or [])])
    for match in re.finditer(r'(?<![\w/])([A-Za-z0-9_@./-]+\.[A-Za-z0-9]+):(\d+)', text):
        file_ref(match[1].removeprefix('./'), int(match[2]))
    return {**binding, 'evidence_refs': refs, 'reference_integrity_verified': True,
            'causal_claim_verified': False}
