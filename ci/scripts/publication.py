#!/usr/bin/env python3
"""Copy publisher evidence unchanged and bind it to one CI run/producer attempt."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re


def prepare(bundle, images, output, env):
    bundle, images, output = Path(bundle), Path(images), Path(output)
    fields = {'source_commit': env['SOURCE_COMMIT'], 'target_id': env['TARGET_ID'],
              'tenant': env['TENANT'], 'app': env['APP']}
    for key, pattern in {'source_commit': r'[a-f0-9]{40}', 'target_id': r'[a-z][a-z0-9-]{0,62}',
                         'tenant': r'[a-z0-9]{1,20}', 'app': r'[a-z][a-z0-9-]{1,28}[a-z0-9]'}.items():
        if not re.fullmatch(pattern, fields[key]):
            raise ValueError('invalid publication identity: ' + key)
    if env['GITHUB_SHA'] != fields['source_commit']:
        raise ValueError('workflow/source commit mismatch')
    receipt = {'version': 1, 'status': 'published', **fields,
               'run_id': int(env['GITHUB_RUN_ID']), 'producer_attempt': int(env['GITHUB_RUN_ATTEMPT']),
               'bundle_artifact_id': int(env['BUNDLE_ARTIFACT_ID'])}
    if any(receipt[key] < 1 for key in ('run_id', 'producer_attempt', 'bundle_artifact_id')):
        raise ValueError('positive producer identifiers required')
    files = {'images.json': images, **{name: bundle / name for name in ('jasmin.yaml', 'verdict.json', 'manifest.json')}}
    if any(path.is_symlink() or not path.is_file() for path in files.values()):
        raise ValueError('publication inputs must be regular files')
    contents = {name: path.read_bytes() for name, path in files.items()}
    receipt['files'] = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    output.mkdir()  # Refuse to mix a previous publication into this attempt.
    for name, data in contents.items():
        (output / name).write_bytes(data)
    (output / 'handoff.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle')
    parser.add_argument('images')
    parser.add_argument('output')
    args = parser.parse_args()
    prepare(args.bundle, args.images, args.output, os.environ)
