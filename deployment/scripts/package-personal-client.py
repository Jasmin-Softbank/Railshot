#!/usr/bin/env python3
"""Build a deterministic, credential-free personal client artifact; never publish."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile


def package(source, output):
    source, output = Path(source), Path(output)
    files = []
    for subtree in ('apps/agent', 'deployment/bootstrap/client_setup', 'infrastructure/providers/openstack'):
        for path in (source / subtree).rglob('*.py'):
            if any(part in ('tests', '__pycache__', '.venv') for part in path.relative_to(source).parts):
                continue
            if path.is_symlink():
                raise ValueError('Artifact symlink rejected')
            if path.is_file():
                files.append(path)
    files.append(source / 'deployment/bootstrap/personal-install.sh')
    required = {'apps/agent/personal.py', 'apps/agent/personal_remove.py', 'apps/agent/openstack_control.py',
                'deployment/bootstrap/client_setup/state.py', 'infrastructure/providers/openstack/cli.py'}
    if not required <= {str(path.relative_to(source)) for path in files}:
        raise ValueError('Required client files missing')
    # Exclusive output creation avoids replacing a reviewed release artifact.
    with output.open('xb') as destination:
        with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for path in sorted(files):
                    data = path.read_bytes()
                    record = tarfile.TarInfo(str(path.relative_to(source)))
                    record.size, record.mode, record.mtime = len(data), 0o644, 0
                    archive.addfile(record, io.BytesIO(data))
    return {'artifact_path': str(output.resolve()), 'artifact_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.source, args.output)))


if __name__ == '__main__':
    main()
