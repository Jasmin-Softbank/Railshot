#!/usr/bin/env python3
"""Install one shared observer on the existing provider pipeline's registered VM."""
import argparse
import base64
import json
import os
from pathlib import Path
import shlex

from register import settings, node_request, observer_ssh, require
from storage import durable_write

HERE = Path(__file__).resolve().parent
# Uploaded source is this reviewed checkout, never source supplied by a product request.
PREPARE = '''import base64,json,pathlib,sys,tempfile
p=json.load(sys.stdin); c=p['config']; out=pathlib.Path(c['observer_directory'])
owner=c['owner']+':'+c['lifecycle']
if out.exists():
    assert out.is_dir() and not out.is_symlink() and (out/'owner').read_text().strip()==owner
else:
    with tempfile.TemporaryDirectory() as staging:
        for name,data in p['files'].items():
            pathlib.Path(staging,name).write_bytes(base64.b64decode(data))
        sys.path.insert(0,staging)
        from render import render_observer
        render_observer({'name':c['owner'],'argocd_metrics':None},out,
                        {'global':{'scrape_interval':'30s','scrape_timeout':'10s'},'scrape_configs':[]},c['observer_ip'])
        (out/'owner').write_text(owner+'\\n')
print(json.dumps({'owner':owner,'prepared':True}))
'''


def bootstrap(config, output):
    from environment import read_private
    import run as ansible
    from argo import native
    config = settings(config)
    request, descriptor = node_request(config['observer_registry_file'], config['observer_target_id'], read_private, ansible)
    receipt = {'status': 'unknown', 'owner': config['owner'], 'lifecycle': config['lifecycle'],
               'resource_id': descriptor['resource_id'], 'role': 'shared_observer', 'collection_state': 'no_targets'}
    durable_write(output, json.dumps(receipt).encode())
    with observer_ssh(config, request, ansible) as prefix:
        payload = {'config': config, 'files': {name: base64.b64encode((HERE / name).read_bytes()).decode()
                                               for name in ('render.py', 'compose.yaml')}}
        # PyYAML is not used by the renderer. Credentials/password are created only on the observer.
        result = json.loads(native([*prefix, 'python3 -c ' + shlex.quote(PREPARE)], document=payload))
        require(result == {'owner': config['owner'] + ':' + config['lifecycle'], 'prepared': True})
        directory = shlex.quote(config['observer_directory'])
        command = ('sudo env DEBIAN_FRONTEND=noninteractive apt-get update -qq && '
                   'sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 && '
                   'sudo systemctl enable --now docker && cd ' + directory +
                   ' && sudo docker compose config --quiet && sudo docker compose up -d')
        require(ansible.execute([*prefix, command], 240, dict(os.environ)) == 0)
        observed = native([*prefix, 'cd ' + directory + ' && sudo docker compose ps --format json'])
        containers = [json.loads(line) for line in observed.splitlines() if line.strip()]
        require({item['Service'] for item in containers if item['State'] == 'running'} == {'prometheus', 'blackbox', 'grafana'})
    receipt['status'] = 'succeeded'
    durable_write(output, json.dumps(receipt).encode())
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        from environment import read_private
        result = bootstrap(read_private(args.config), Path(args.out))
    except Exception as error:
        print(json.dumps({'status': 'unknown', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
