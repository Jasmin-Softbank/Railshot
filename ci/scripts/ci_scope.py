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
# Native controller files copied into the API stage, in addition to apps/api and dashboard assets.
API_NATIVE_FILES = {
    'observability/register.py', 'observability/bootstrap.py', 'observability/render.py', 'observability/compose.yaml', 'observability/runtime_health.py',
    'gitops/bridge.py', 'gitops/argo.py', 'gitops/handoff.py', 'gitops/credentials.py',
    'gitops/edge.py', 'gitops/service_name.py', 'gitops/logs.py',
    'gitops/dns.py', 'gitops/gcp_routes.py', 'gitops/openstack_routes.py', 'gitops/application_cleanup.py',
    'ci/scripts/execution.py', 'ci/scripts/observability.py', 'ci/scripts/process.py',
    'ci/scripts/publication.py', 'ci/scripts/storage.py', 'ci/scripts/gate/bundle.py',
    'ci/scripts/runner/runtime_boundary.py', 'ci/scripts/runner/replenish.py', 'ci/scripts/schemas/railshot.schema.json',
    'ci/requirements-dev.txt', 'ci/requirements-test.txt',
    'infrastructure/ansible/run.py', 'infrastructure/ansible/transport.py',
    'infrastructure/ansible/ansible.cfg', 'infrastructure/ansible/guest.yml',
    'infrastructure/ansible/runtime.yml', 'infrastructure/ansible/tasks/guest-checks.yml',
    'infrastructure/ansible/group_vars/all.yml', 'contracts/ansible-request.schema.json',
    'contracts/ansible-job.schema.json', 'infrastructure/ansible/cluster.py',
    'infrastructure/ansible/database.py', 'infrastructure/ansible/inputs.py',
    'infrastructure/ansible/application_database.py', 'infrastructure/ansible/database.yml',
    'infrastructure/ansible/application-database.yml',
    'deployment/scripts/common.sh', 'deployment/scripts/environment.py', 'deployment/bootstrap/preflight.sh',
    'deployment/scripts/applications.py', 'deployment/scripts/application_release.py', 'deployment/scripts/application_routes.py',
    'deployment/scripts/openstack_route_worker.py', 'deployment/scripts/application_lifecycle.py',
    'deployment/scripts/lifecycle_runtime.py',
    'deployment/cloudflared/register.py', 'deployment/cloudflared/render.py',
    'deployment/bootstrap/install-k3s.sh', 'deployment/bootstrap/health.sh', 'deployment/bootstrap/runtime-healthz.py',
    'deployment/cilium/install.sh', 'deployment/cilium/preflight.py',
    'deployment/cilium/health.sh', 'deployment/airgap/versions.json',
}
API_NATIVE_PREFIXES = ('infrastructure/ansible/roles/', 'infrastructure/ansible/playbooks/',
                       'infrastructure/providers/terraform_tools/',
                       'infrastructure/terraform/aws/', 'infrastructure/terraform/gcp/',
                       'infrastructure/terraform/aws-edge/', 'infrastructure/terraform/gcp-edge/')


def api_native_dependency(path):
    return path in API_NATIVE_FILES or path.startswith(API_NATIVE_PREFIXES)


def documentation(path):
    return (path.startswith('docs/') and PurePosixPath(path).suffix in
            {'.md', '.txt', '.svg', '.png', '.jpg', '.jpeg', '.pdf', '.drawio', '.mmd'}
            or PurePosixPath(path).name in {'README.md', 'README.ko.md', 'AGENT.md', 'AGENTS.md', 'LICENSE'})


def release_required(paths):
    """Release common runtime/worker/IaC policy too; only proven docs-only diffs skip."""
    return paths is None or any(not path or path.startswith('/') or '..' in PurePosixPath(path).parts
                                or not documentation(path) for path in paths)


def container_components(paths):
    components = set()
    for path in paths:
        if documentation(path) or path == 'docs/api/product.openapi.json':
            continue
        if api_native_dependency(path):
            components.add('api')
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
        elif path in {'deployment/scripts/render-platform.py', 'deployment/scripts/tests/test_platform.py', 'deployment/manifests/build-controller.yaml'}:
            components.update(COMPONENTS)
        elif path in {'deployment/compose.yaml', 'deployment/.env.example',
                      'deployment/manifests/platform.yaml',
                      'gitops/applications/railshot-platform.yaml'}:
            components.update(('dashboard', 'api', 'mcp'))
        elif (path.startswith(('apps/agent/', 'ci/', 'deployment/', 'infrastructure/ansible/',
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
        if path == 'docs/api/product.openapi.json':
            selected.update(('contracts', 'api-browser'))
        elif path == 'docs/api/ansible.openapi.json' or path.startswith('examples/ansible/'):
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
        elif path.startswith(('apps/agent/', 'deployment/bootstrap/client_setup/',
                              'deployment/bootstrap/templates/')) or path in {
                'deployment/bootstrap/install.sh', 'deployment/bootstrap/uninstall.sh',
                'deployment/bootstrap/install_payload.py', 'deployment/bootstrap/requirements.lock'}:
            selected.add('openstack')
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
                      'deployment/manifests/platform.yaml', 'deployment/manifests/build-runner.yaml', 'deployment/manifests/build-controller.yaml', 'deployment/scripts/render-platform.py',
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
        if api_native_dependency(path):
            selected.add('api-browser')
        if path == 'apps/api/src/metrics.js':
            selected.add('observability')
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
    release = release_required(paths)
    # A trusted automatic release exports all images once, even when only the
    # native runtime/edge/worker source changes. The gate must require that job.
    if release and os.environ.get('AUTO_RELEASE') == 'true':
        selected.add('containers')
        components = set(COMPONENTS)
    result = json.dumps([job for job in JOBS if job in selected])
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(f'selected={result}\n')
        stream.write(f'release={str(release).lower()}\n')
        stream.write('container_components=' + json.dumps([name for name in COMPONENTS if name in components]) + '\n')
        for job in JOBS:
            stream.write(f'{job}={str(job in selected).lower()}\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
        stream.write(f'## Change scope\n\nSelected checks: `{result}`.\n\n')
        stream.write('Full validation (manual/unknown event or unavailable diff boundary).\n'
                     if paths is None else f'Compared {len(paths)} changed paths, including deletions and rename sources.\n')


if __name__ == '__main__':
    main()
