#!/usr/bin/env python3
"""Select repository checks from a complete Git diff; unknown paths run every check."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

JOBS = ('contracts', 'openstack', 'database-ansible', 'api-browser', 'terraform',
        'runtime-smoke', 'observability', 'containers')
COMPONENTS = ('dashboard', 'api', 'mcp', 'ci-runner')
SHA = re.compile(r'[0-9a-f]{40}')


def documentation(path):
    return (path.startswith('docs/') and PurePosixPath(path).suffix in
            {'.md', '.txt', '.svg', '.png', '.jpg', '.jpeg', '.pdf', '.drawio', '.mmd'}
            or PurePosixPath(path).name in {'README.md', 'README.ko.md', 'AGENT.md', 'AGENTS.md', 'LICENSE'})


def container_components(paths):
    components = set()
    for path in paths:
        if documentation(path):
            continue
        if path.startswith(('.github/', 'contracts/')) or path in {
                '.dockerignore', 'ci/scripts/container-smoke.py'}:
            components.update(COMPONENTS)
        elif path in {'package.json', 'package-lock.json', 'apps/api/package.json',
                      'apps/dashboard/package.json'}:
            components.update(('dashboard', 'api', 'mcp'))
        elif path.startswith('apps/dashboard/'):
            components.update(('dashboard', 'api'))  # API image retains source asset routes.
        elif path.startswith('apps/api/'):
            components.update(('api', 'mcp'))
        elif path.startswith('ci/scripts/') or path == 'ci/runner-compose.yml':
            components.add('ci-runner')  # The runner image COPYs all CI scripts.
        elif path == 'deployment/manifests/build-runner.yaml' or path == 'infrastructure/ansible/ci.yml':
            components.add('ci-runner')
        elif path in {'deployment/scripts/render-platform.py', 'deployment/scripts/tests/test_platform.py'}:
            components.update(COMPONENTS)
        elif path in {'deployment/compose.yaml', 'deployment/.env.example',
                      'deployment/manifests/platform.yaml',
                      'gitops/applications/railshot-platform.yaml'}:
            components.update(('dashboard', 'api', 'mcp'))
        elif (path.startswith(('ci/', 'deployment/', 'infrastructure/ansible/',
                               'infrastructure/providers/openstack/',
                               'infrastructure/providers/terraform_tools/',
                               'infrastructure/terraform/', 'gitops/', 'observability/',
                               'examples/ansible/')) or path == 'docs/api/ansible.openapi.json'):
            continue
        else:
            components.update(COMPONENTS)
    return components


def select(paths):
    selected = set()
    for path in paths:
        # Do not interpret filenames as patterns, shell input or Git options.
        parts = PurePosixPath(path).parts
        if not path or path.startswith('/') or '..' in parts:
            return set(JOBS)
        if path == 'docs/api/ansible.openapi.json' or path.startswith('examples/ansible/'):
            selected.add('contracts')  # These documents are executable test fixtures.
        elif documentation(path):
            continue
        elif path.startswith(('.github/', 'contracts/')):
            selected.update(JOBS)
        elif path in {'package.json', 'package-lock.json'}:
            selected.add('api-browser')
        elif path == '.dockerignore':
            continue  # Container job below checks all affected image contexts.
        elif path.startswith(('apps/api/', 'apps/dashboard/', 'ci/browser/')):
            selected.add('api-browser')
        elif path.startswith('infrastructure/providers/openstack/'):
            selected.update(('openstack', 'contracts'))
        elif path.startswith('infrastructure/providers/terraform_tools/'):
            selected.update(('contracts', 'terraform'))
        elif path.startswith('infrastructure/terraform/'):
            selected.update(('terraform', 'contracts'))
        elif path.startswith('infrastructure/ansible/'):
            selected.update(('contracts', 'database-ansible'))
            if path == 'infrastructure/ansible/ci.yml':
                selected.add('terraform')  # CI VM bootstrap consumes this playbook.
        elif path in {'deployment/compose.yaml', 'deployment/.env.example',
                      'deployment/manifests/platform.yaml', 'deployment/manifests/build-runner.yaml', 'deployment/scripts/render-platform.py',
                      'deployment/scripts/tests/test_platform.py'}:
            selected.add('contracts')  # Platform workloads do not install the customer runtime.
        elif path.startswith('deployment/'):
            selected.update(('contracts', 'runtime-smoke'))
        elif path.startswith('gitops/'):
            selected.add('contracts')
        elif path.startswith('observability/'):
            selected.add('observability')
        elif path.startswith('ci/'):
            selected.update(JOBS)  # Shared CI scripts/workflows serve several consumers.
        else:
            selected.update(JOBS)
    selected.discard('containers')
    if container_components(paths):
        selected.add('containers')
    return selected


def git(*args, cwd):
    return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.PIPE)


def changed_paths(event_name, event, cwd):
    """None means run all; [] is a successfully compared, empty change set."""
    if event_name == 'pull_request':
        base = event['pull_request']['base']['sha']
        head = event['pull_request']['head']['sha']
    elif event_name == 'push':
        base, head = event['before'], event['after']
    else:
        return None
    if not all(isinstance(ref, str) and SHA.fullmatch(ref) for ref in (base, head)):
        raise ValueError('Expected full Git commit SHA boundaries')
    if '0' * 40 in (base, head):
        return None  # New/deleted branch has no comparable prior tree.
    try:
        if event_name == 'pull_request':
            base = git('merge-base', base, head, cwd=cwd).decode().strip()
        # --no-renames reports both old and new paths, including cross-scope moves.
        output = git('diff', '--name-only', '--no-renames', '-z', base, head, '--', cwd=cwd)
    except subprocess.CalledProcessError:
        return None  # E.g. a force-pushed base is unavailable: validate everything.
    return [os.fsdecode(path) for path in output.split(b'\0') if path]


def validate_gate(checks):
    if set(checks) != set(JOBS) | {'changes'}:
        raise ValueError('Gate dependencies do not match the complete check set')
    if checks['changes']['result'] != 'success':
        raise ValueError('Change selection failed or was cancelled')
    selected = json.loads(checks['changes']['outputs']['selected'])
    if (not isinstance(selected, list) or not all(isinstance(job, str) for job in selected)
            or len(selected) != len(set(selected)) or not set(selected) <= set(JOBS)):
        raise ValueError('Invalid selected check set')
    for job in JOBS:
        expected = 'success' if job in selected else 'skipped'
        if checks[job]['result'] != expected:
            raise ValueError(f'{job}: expected {expected}, got {checks[job]["result"]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('select', 'gate'))
    args = parser.parse_args()
    if args.mode == 'gate':
        checks = json.loads(os.environ['RESULTS'])
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('| Check | Result |\n|---|---|\n')
            for job, check in checks.items():
                stream.write(f'| {job} | {check["result"]} |\n')
            stream.write('\nSkipped checks must be outside the selected change scope. '
                         'HTTP tests mock external services; runtime E2E uses a disposable '
                         'runner and cleanup. Neither proves product-to-cloud automation.\n')
        validate_gate(checks)
        return
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    paths = changed_paths(os.environ['GITHUB_EVENT_NAME'], event, Path.cwd())
    selected = set(JOBS) if paths is None else select(paths)
    components = set(COMPONENTS) if paths is None else container_components(paths)
    result = json.dumps([job for job in JOBS if job in selected])
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(f'selected={result}\n')
        stream.write('container_components=' + json.dumps([name for name in COMPONENTS if name in components]) + '\n')
        for job in JOBS:
            stream.write(f'{job}={str(job in selected).lower()}\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
        stream.write(f'## Change scope\n\nSelected checks: `{result}`.\n\n')
        stream.write('Full validation (manual/unknown event or unavailable diff boundary).\n'
                     if paths is None else f'Compared {len(paths)} changed paths, including deletions and rename sources.\n')


if __name__ == '__main__':
    main()
