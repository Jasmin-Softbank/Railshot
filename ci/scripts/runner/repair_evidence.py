"""Bind a proposal to host-captured failure facts. Text is never repair authority."""
import hashlib
import json
from pathlib import Path
import re

from source_snapshot import capture, entries_digest


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(run):
    path = Path(run) / 'diagnostics/case.json'
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('repair evidence invalid')
    return path.read_bytes()


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
                if not item or not re.fullmatch(r'process-[1-9][0-9]*\.log', item['path']):
                    raise ValueError('repair evidence log reference invalid')
                path = Path(run) / 'diagnostics' / item['path']
                if path.is_symlink() or path.stat().st_size > 65536:
                    raise ValueError('repair evidence log invalid')
                data = path.read_bytes()
                if sha(data) != item['sha256']:
                    raise ValueError('repair evidence log changed')
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
