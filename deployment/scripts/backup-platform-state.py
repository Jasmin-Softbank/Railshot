#!/usr/bin/env python3
"""Snapshot platform SQLite + private recovery files, or restore into a new drill directory.

SQLite backup is online-consistent per database. For one platform recovery point,
quiesce API/Terraform/edge/billing writers before capture using the existing release
and bootstrap freeze procedures. This helper never stops writers or overwrites live
state. Upload the resulting private directory with encryption outside the node;
connections.key and source/config files must accompany the databases.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import sys
import time

class Blocked(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Blocked(code)


SCOPES = {'state', 'cd', 'infra', 'edge', 'budget', 'registration', 'terraform', 'billing',
          'config', 'environments', 'control-terraform', 'applications', 'provider-edges', 'reconciliations', 'tunnel-state'}
SKIP = {'owner.json', '.terraform.tfstate.lock.info', '.terraform', '.git'}


def private_directory(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), 'PRIVATE_DIRECTORY_REQUIRED')
    info = path.stat()
    # Kubelet requires group access on the PVC root for fsGroup=1000. Private
    # state below it stays 0700/0600; accepting this root avoids recursive chmod.
    private = not info.st_mode & 0o077 or (not info.st_mode & 0o007 and info.st_uid == info.st_gid == 1000)
    require(stat.S_ISDIR(info.st_mode) and private
            and os.geteuid() in (0, info.st_uid), 'PRIVATE_DIRECTORY_REQUIRED')
    return path


def safe_file(path, root):
    info = path.lstat()
    require(path.resolve() == path and stat.S_ISREG(info.st_mode)
            and info.st_uid in (os.geteuid(), root.stat().st_uid) and not info.st_mode & 0o022,
            'UNSAFE_STATE_FILE')
    return info


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root):
    files = []
    for name in sorted(SCOPES):
        directory = root / name
        if not directory.exists():
            continue
        require(directory.is_dir() and directory.resolve() == directory, 'UNSAFE_STATE_DIRECTORY')
        for home, directories, names in os.walk(directory, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in SKIP)
            for child in directories:
                require(not (Path(home) / child).is_symlink(), 'UNSAFE_STATE_DIRECTORY')
            for name in sorted(names):
                if name in SKIP or name.endswith(('.lock', '-wal', '-shm', '.tmp')):
                    continue
                path = Path(home) / name
                safe_file(path, root)
                files.append(path)
    require(root / 'state/dashboard.sqlite3' in files and root / 'state/connections.key' in files,
            'API_DATABASE_AND_KEY_REQUIRED')
    return files


def verify_database(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as database:
        require(database.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
                and not database.execute('PRAGMA foreign_key_check').fetchall(), 'DATABASE_INTEGRITY_FAILED')


def database_backup(source, destination):
    # The native backup API includes committed WAL pages without copying active WAL files.
    deadline = time.monotonic() + 60
    def progress(_status, _remaining, _total):
        require(time.monotonic() < deadline, 'DATABASE_BACKUP_TIMEOUT')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as incoming, closing(sqlite3.connect(destination)) as outgoing:
        incoming.backup(outgoing, pages=256, progress=progress, sleep=0.05)
        outgoing.execute('PRAGMA journal_mode=DELETE')
    verify_database(destination)


def new_destination(path, source):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and path != source
            and source not in path.parents and not path.exists(), 'NEW_DESTINATION_REQUIRED')
    private_directory(path.parent)
    path.mkdir(mode=0o700)
    return path


def backup(root, destination):
    root = private_directory(root)
    files = inventory(root)
    destination = new_destination(destination, root)
    try:
        records = []
        for source in files:
            before = safe_file(source, root)
            target = destination / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open('xb') as stream:
                os.chmod(target, 0o600)
            with source.open('rb') as stream:
                database = stream.read(16) == b'SQLite format 3\x00'
            require(database or source.suffix not in {'.sqlite', '.sqlite3', '.db'}, 'DATABASE_HEADER_INVALID')
            if database:
                database_backup(source, target)
            else:
                shutil.copyfile(source, target)
                after = safe_file(source, root)
                require((before.st_ino, before.st_size, before.st_mtime_ns) ==
                        (after.st_ino, after.st_size, after.st_mtime_ns)
                        and checksum(source) == checksum(target), 'SOURCE_CHANGED_DURING_BACKUP')
            with target.open('rb') as stream:
                os.fsync(stream.fileno())
            records.append({'path': source.relative_to(root).as_posix(), 'kind': 'sqlite' if database else 'file',
                            'sha256': checksum(target), 'bytes': target.stat().st_size})
        require(files == inventory(root), 'SOURCE_INVENTORY_CHANGED')
        manifest = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'consistency': 'online_per_database', 'files': records}
        for directory in [destination, *(path for path in destination.rglob('*') if path.is_dir())]:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        with (destination / 'manifest.json').open('x') as stream:
            os.chmod(stream.name, 0o600)
            json.dump(manifest, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {'status': 'backed_up', 'files': len(records), 'databases': sum(item['kind'] == 'sqlite' for item in records),
                'manifest_sha256': checksum(destination / 'manifest.json')}
    except BaseException:
        shutil.rmtree(destination)
        raise


def restore_drill(source, destination):
    source = private_directory(source)
    safe_file(source / 'manifest.json', source)
    manifest = json.loads((source / 'manifest.json').read_text())
    require(manifest.get('version') == 1 and isinstance(manifest.get('files'), list), 'BACKUP_MANIFEST_INVALID')
    names = set()
    for item in manifest['files']:
        path = PurePosixPath(item['path'])
        require(path.as_posix() == item['path'] and not path.is_absolute() and '..' not in path.parts
                and path.parts[0] in SCOPES and item['path'] not in names
                and not any(part in SKIP for part in path.parts) and item['kind'] in ('sqlite', 'file'), 'BACKUP_PATH_INVALID')
        names.add(item['path'])
        file = source / item['path']
        safe_file(file, source)
        require(file.stat().st_size == item['bytes'] and checksum(file) == item['sha256'], 'BACKUP_CHECKSUM_FAILED')
    require({'state/dashboard.sqlite3', 'state/connections.key'} <= names
            and any(item['path'] == 'state/dashboard.sqlite3' and item['kind'] == 'sqlite' for item in manifest['files']),
            'API_DATABASE_AND_KEY_REQUIRED')
    destination = new_destination(destination, source)
    try:
        for item in manifest['files']:
            target = destination / item['path']
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source / item['path'], target)
            os.chmod(target, 0o600)
            require(checksum(target) == item['sha256'], 'RESTORE_CHECKSUM_FAILED')
            if item['kind'] == 'sqlite':
                verify_database(target)
        return {'status': 'restore_drill_verified', 'files': len(names),
                'databases': sum(item['kind'] == 'sqlite' for item in manifest['files']),
                'manifest_sha256': checksum(source / 'manifest.json')}
    except BaseException:
        shutil.rmtree(destination)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup', 'restore-drill'))
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = (backup if args.action == 'backup' else restore_drill)(args.source, args.destination)
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'code': str(error) if isinstance(error, Blocked) else 'STATE_BACKUP_FAILED'}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
