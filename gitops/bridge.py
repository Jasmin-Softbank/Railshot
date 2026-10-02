#!/usr/bin/env python3
"""Private product-backend bridge. stdin is trusted publication data, never shell input."""
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import argo
import edge
import handoff
from storage import durable_write

FILES = (*handoff.FILES, 'handoff.json')
ID = r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def private_directory(path):
    path = Path(path)
    handoff.require(path.is_absolute() and not path.is_symlink(), 'private absolute directory required')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    handoff.require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'private owned directory required')
    return path


def read_config(path):
    path = Path(path)
    info = path.lstat()
    handoff.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                    not info.st_mode & 0o077 and info.st_size <= 1_000_000, 'private operator config required')
    raw = path.read_bytes()
    config = json.loads(raw)
    handoff.require(set(config) == {'version', 'state_dir', 'repository', 'branch', 'context', 'targets'} and
                    config['version'] == 1 and isinstance(config['targets'], dict), 'operator config v1 required')
    handoff.require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/._-]{0,127}', config['branch']) and
                    '..' not in config['branch'] and not config['branch'].endswith('/'), 'registered branch required')
    handoff.require(Path(config['repository']).is_absolute(), 'registered absolute config checkout required')
    config['_sha256'] = hashlib.sha256(raw).hexdigest()
    return config


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def public_probe(config, health_path):
    """Observe exactly the operator-approved HTTPS URL; no proxy, redirect, or body echo."""
    handoff.require(set(config) == {'url', 'expected_json'}, 'registered public health expectation required')
    url = argo.https_url(config['url'])
    parsed = urlsplit(url)
    handoff.require(parsed.path == health_path and isinstance(config['expected_json'], dict) and
                    len(encoded(config['expected_json'])) <= 65536, 'public health path/JSON expectation required')
    result = {'state': 'unverified', 'verified_at': None, 'url': None}
    try:
        request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'railshot-health/1'})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(65537)
            ok = response.status == 200 and len(raw) <= 65536 and encoded(json.loads(raw)) == encoded(config['expected_json'])
        if ok:
            result.update(state='succeeded', verified_at=datetime.now(timezone.utc).isoformat(), url=url)
    except (OSError, ValueError, HTTPError, URLError):
        pass
    return result


def site_probe(url):
    """The generated site path must itself serve HTTPS 200 without a redirect."""
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
                Request(argo.https_url(url), headers={'User-Agent': 'railshot-health/1'}), timeout=10) as response:
            response.read(1)
            return response.status == 200
    except (OSError, ValueError, HTTPError, URLError):
        return False


def output(cd='blocked', *, revision=None, deployed=False, public=None, code=None, unknown=False):
    value = {'cd': {'state': cd, 'revision': revision, 'deployed': deployed},
             'public_http': public or {'state': 'not_run', 'verified_at': None, 'url': None}}
    if code:
        value['error'] = {'code': code, 'retryable': False, 'outcome_unknown': unknown}
    return value


def git(config, *args):
    # The dedicated operator checkout supplies credentials; no user-provided Git command or remote.
    return argo.native(['git', '-C', config['repository'], '-c', 'core.hooksPath=/dev/null', *args]).strip()


def validate_request(config, request):
    handoff.require(isinstance(request, dict) and set(request) == {
        'action', 'deployment_id', 'target_id', 'config_sha256', 'publication', 'files'} and
        request['action'] in ('apply', 'observe') and re.fullmatch(ID, request['deployment_id']),
        'bounded bridge request required')
    handoff.require(isinstance(request['config_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', request['config_sha256']) and
                    request['config_sha256'] == config['_sha256'], 'registered config changed after admission')
    publication = request['publication']
    handoff.require(isinstance(publication, dict) and request['target_id'] == publication.get('target_id'),
                    'publication target mismatch')
    registered = config['targets'].get(request['target_id'])
    handoff.require(isinstance(registered, dict) and {'target', 'app', 'tenant', 'public_http'} <= set(registered) <=
                    {'target', 'app', 'tenant', 'public_http', 'edge'},
                    'registered deployment target required')
    handoff.require(registered['target']['id'] == request['target_id'] and
                    publication.get('app') == registered['app'] and publication.get('tenant') == registered['tenant'],
                    'registered application/tenant mismatch')
    if 'edge' in registered:
        edge.validate_binding(registered['edge'], registered)
    handoff.require(isinstance(request['files'], dict) and set(request['files']) == set(FILES),
                    'exact trusted publication files required')
    files = {}
    for name, value in request['files'].items():
        handoff.require(isinstance(value, str) and len(value) < 2_700_000, 'bounded base64 file required')
        files[name] = base64.b64decode(value, validate=True)
        handoff.require(len(files[name]) < 2_000_000, 'bounded publication file required')
    receipt = json.loads(files['handoff.json'])
    handoff.require(all(publication.get(k) == v for k, v in receipt.items()) and
                    publication.get('images') == json.loads(files['images.json']) and
                    type(publication.get('artifact_id')) is int and publication['artifact_id'] > 0,
                    'verified publication receipt and artifact identity required')
    binding = {'publication': publication, 'config_sha256': request['config_sha256'],
               'target': registered, 'repository': config['repository'],
               'branch': config['branch'], 'context': config['context']}
    return registered, files, hashlib.sha256(encoded(binding)).hexdigest()


def observe(config, registered, directory, state):
    review = argo.load_review(directory / 'review')
    argo.verify_git(review, config['repository'])
    app = review['application']
    project = argo.kubectl(config['context'], app['metadata']['namespace'],
                          'get', 'appproject', app['spec']['project'], '-o', 'json')
    argo.validate_project(project, app)
    # sync=False is read-only even after an interrupted push or Argo operation.
    observed = argo.deploy(review, config['context'], sync=False, timeout=0)
    public = None
    if observed['deployed']:
        if 'edge' in registered:
            try:
                public = edge.observe(registered['edge'], state['edge_request'], observed['git_revision'],
                                      public_probe, site_probe, review['receipt']['http']['route'])
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                public = {'state': 'unverified', 'verified_at': None, 'url': None}
        else:
            public = public_probe(registered['public_http'], review['receipt']['http']['health_path'])
    return output(observed['status'], revision=observed['git_revision'], deployed=observed['deployed'], public=public)


def execute(config, request):
    registered, files, binding = validate_request(config, request)
    root = private_directory(config['state_dir'])
    # ponytail: one config-repo writer; use separate registered repos before adding parallel workers.
    with os.fdopen(os.open(root / 'bridge.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return output(code='CD_EXECUTOR_BUSY')
        directory = root / request['deployment_id']
        state_path = directory / 'state.json'
        if directory.exists():
            private_directory(directory)
            if not state_path.is_file() or state_path.is_symlink():
                return output('unknown', code='CD_RECONCILE_REQUIRED', unknown=True)
            state = json.loads(state_path.read_bytes())
            handoff.require(state['binding'] == binding, 'deployment binding conflict')
            if state.get('phase') == 'blocked':
                return output(code='CD_PREPARATION_FAILED')
            try:
                return observe(config, registered, directory, state)
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                return output('unknown', revision=state.get('revision'), code='CD_RECONCILE_REQUIRED', unknown=True)
        if request['action'] == 'observe':
            return output(code='DEPLOYMENT_NOT_FOUND')
        directory = private_directory(directory)
        state = {'binding': binding, 'phase': 'preparing', 'revision': None}
        if 'edge' in registered:
            state['edge_request'] = {'deployment_id': request['deployment_id'], 'publication': request['publication']}
        def save(phase):
            state['phase'] = phase
            durable_write(state_path, encoded(state))
        save('preparing')
        try:
            published = private_directory(directory / 'published')
            for name, raw in files.items():
                durable_write(published / name, raw)
            target = dict(registered['target'])
            handoff.require(git(config, 'branch', '--show-current') == config['branch'] and
                            not git(config, 'status', '--porcelain'), 'dedicated clean registered branch required')
            remote = argo.https_url(git(config, 'remote', 'get-url', 'origin'))
            handoff.require(remote.rstrip('/').removesuffix('.git') == target['repo_url'].rstrip('/').removesuffix('.git'),
                            'registered Git remote mismatch')
            git(config, 'fetch', '--no-tags', 'origin', config['branch'])
            base = git(config, 'rev-parse', 'HEAD')
            handoff.require(base == git(config, 'rev-parse', 'FETCH_HEAD'), 'config checkout must match remote branch')
            target['revision'] = base
            rendered = handoff.render(published, target)
            # Validate the public contract before any remote mutation.
            public = registered['public_http']
            handoff.require(set(public) == {'url', 'expected_json'} and isinstance(public['expected_json'], dict) and
                            len(encoded(public['expected_json'])) <= 65536 and
                            urlsplit(argo.https_url(public['url'])).path == rendered['http']['health_path'],
                            'registered HTTPS health expectation required')
            app = rendered['application']
            project = argo.kubectl(config['context'], app['metadata']['namespace'],
                                  'get', 'appproject', app['spec']['project'], '-o', 'json')
            argo.validate_project(project, app)
            repository = Path(config['repository']).resolve()
            workload_dir = repository / target['path']
            handoff.require(repository in workload_dir.resolve().parents and
                            all(not p.is_symlink() for p in (workload_dir, *workload_dir.parents) if p != repository),
                            'regular repository-owned application path required')
            workload_dir.mkdir(parents=True, exist_ok=True)
            handoff.require(set(p.name for p in workload_dir.iterdir()) <= {'workload.json'}, 'dedicated application path required')
            workload_file = workload_dir / 'workload.json'
            if workload_file.exists():
                handoff.require(workload_file.is_file() and not workload_file.is_symlink() and
                                workload_file.stat().st_size < 2_000_000, 'regular owned workload required')
                prior = json.loads(workload_file.read_bytes())
                handoff.require(prior.get('kind') == 'List' and len(prior['items']) == 3 and
                                {item['kind'] for item in prior['items']} == {item['kind'] for item in argo.KINDS} and
                                all(item['metadata'] == {'name': registered['app'], 'namespace': target['namespace']}
                                    for item in prior['items']), 'existing Git workload belongs to another application')
                prior_deployment = next(item for item in prior['items'] if item['kind'] == 'Deployment')
                handoff.require(prior_deployment['spec']['template']['metadata']['labels'].get('railshot.io/target') == target['id'],
                                'existing Git workload target differs')
            durable_write(workload_file, encoded(rendered['workload']))
            tracked_path = target['path'] + '/workload.json'
            git(config, 'add', '--', tracked_path)
            staged = git(config, 'diff', '--cached', '--name-only').splitlines()
            handoff.require(staged in ([], [tracked_path]), 'only the registered workload may be committed')
            if staged:
                git(config, 'commit', '-m', 'Deploy ' + registered['app'] + ' (' + request['deployment_id'] + ')')
            state['revision'] = git(config, 'rev-parse', 'HEAD')
            target['revision'] = state['revision']
            rendered = handoff.render(published, target)
            review_dir = private_directory(directory / 'review')
            for name in ('application', 'workload'):
                durable_write(review_dir / (name + '.json'), encoded(rendered.pop(name)))
            durable_write(review_dir / 'receipt.json', encoded(rendered))
            review = argo.load_review(review_dir)
            argo.verify_git(review, config['repository'])
            save('pushing')  # Persist intent before the first external mutation. Never blindly retry it.
            git(config, 'push', 'origin', state['revision'] + ':refs/heads/' + config['branch'])
            handoff.require(git(config, 'ls-remote', 'origin', 'refs/heads/' + config['branch']).split()[0] == state['revision'],
                            'remote revision readback differs')
            save('syncing')
            argo.deploy(review, config['context'], sync=True, timeout=0)
            if 'edge' in registered:
                save('routing')
                edge.ensure(registered['edge'])
            save('observing')
            return observe(config, registered, directory, state)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, handoff.ValidationError):
            unknown = state['phase'] in ('pushing', 'syncing', 'routing', 'observing')
            if not unknown:
                save('blocked')
            return output('unknown' if unknown else 'blocked', revision=state['revision'],
                          code='CD_RECONCILE_REQUIRED' if unknown else 'CD_PREPARATION_FAILED', unknown=unknown)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(14_000_001)
        handoff.require(len(raw) <= 14_000_000, 'bounded bridge input required')
        result = execute(read_config(args.config), json.loads(raw))
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, handoff.ValidationError):
        result = output(code='CD_BRIDGE_INPUT_INVALID')
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
