#!/usr/bin/env python3
"""Private backup bundle validation; authenticity still requires a restore drill."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile

from recovery_service import Custody, canonical, exclusive_write, fsync_directory, load_keyring, private_directory, private_read, identifier


def snapshot_verify(path):
    # Never extract untrusted archive entries or accept a header as proof.
    with tarfile.open(path, "r:gz") as archive:
        entries = archive.getmembers()
        names = {entry.name for entry in entries}
        required = {"meta.json", "state.bin", "SHA256SUMS"}
        if not required <= names or names - required - {"SHA256SUMS.sealed"} or len(entries) != len(names) or any(not e.isfile() or e.size > 4 * 1024**3 for e in entries):
            raise ValueError("invalid Raft snapshot members")
        metadata = json.load(archive.extractfile("meta.json"))
        if not isinstance(metadata, dict) or not {"ID", "Index", "Term", "Size"} <= set(metadata) or any(type(metadata[k]) is not int or metadata[k] < 0 for k in ("Index", "Term", "Size")):
            raise ValueError("invalid Raft snapshot metadata")
        if archive.getmember("state.bin").size != metadata["Size"]:
            raise ValueError("Raft snapshot size mismatch")
        checksums = archive.extractfile("SHA256SUMS").read(65537)
        if len(checksums) > 65536:
            raise ValueError("snapshot checksum limit")
        expected = {}
        for line in checksums.decode().splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})\s+\*?([^\s]+)", line)
            if not match or match[2] not in ("meta.json", "state.bin") or match[2] in expected:
                raise ValueError("invalid checksum entry")
            expected[match[2]] = match[1]
        if set(expected) != {"meta.json", "state.bin"}:
            raise ValueError("incomplete checksums")
        for name, digest in expected.items():
            actual = hashlib.file_digest(archive.extractfile(name), "sha256").hexdigest()
            if digest != actual:
                raise ValueError("snapshot checksum mismatch")
    return metadata["Index"]


def custody_verify(path, keyring):
    active, keys = load_keyring(keyring)
    custody = object.__new__(Custody)
    custody.active_key_id, custody.keys = active, keys
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("custody integrity failed")
        if {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} != {"escrows"}:
            raise ValueError("unexpected custody tables")
        if [r[1] for r in db.execute("PRAGMA table_info(escrows)")] != ["receipt_id", "environment_id", "kind", "operation_id", "key_id", "nonce", "ciphertext"]:
            raise ValueError("unexpected custody schema")
        used = set()
        for row in db.execute("SELECT * FROM escrows"):
            if not isinstance(row["nonce"], bytes) or len(row["nonce"]) != 12 or not isinstance(row["ciphertext"], bytes) or len(row["ciphertext"]) > 65552:
                raise ValueError("invalid custody ciphertext")
            custody.decrypt(row)
            used.add(row["key_id"])
        return sorted(used)
    finally:
        db.close()


def validate_reference(path):
    value = json.loads(private_read(path))
    if not isinstance(value, dict) or set(value) != {"version", "vault_version", "transit_key_ids"} or value["version"] != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", value["vault_version"]):
        raise ValueError("invalid backup reference")
    if not isinstance(value["transit_key_ids"], list) or len(value["transit_key_ids"]) > 10000 or len(set(value["transit_key_ids"])) != len(value["transit_key_ids"]):
        raise ValueError("invalid transit key references")
    for key in value["transit_key_ids"]:
        if not isinstance(key, str) or not re.fullmatch(r"railshot-[a-z0-9][a-z0-9-]{0,62}", key):
            raise ValueError("invalid transit key identifier")
    return value


def safe_input(path):
    # Huge snapshots are streamed; enforce private regular input without reading it.
    import stat
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("unsafe backup input")


def verify_bundle(bundle, keyring):
    private_directory(bundle)
    expected_files = {"vault.snapshot", "custody.sqlite3", "config-reference.json", "manifest.json"}
    if {p.name for p in Path(bundle).iterdir()} != expected_files:
        raise ValueError("unexpected backup files")
    for name in expected_files:
        safe_input(Path(bundle) / name)
    manifest = json.loads(private_read(Path(bundle) / "manifest.json"))
    if set(manifest) != {"version", "files", "custody_key_ids", "snapshot_index"} or manifest["version"] != 1 or set(manifest["files"]) != expected_files - {"manifest.json"}:
        raise ValueError("invalid bundle manifest")
    for name, expected in manifest["files"].items():
        with open(Path(bundle) / name, "rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("bundle hash mismatch")
    validate_reference(Path(bundle) / "config-reference.json")
    if custody_verify(Path(bundle) / "custody.sqlite3", keyring) != manifest["custody_key_ids"]:
        raise ValueError("custody key inventory mismatch")
    if snapshot_verify(Path(bundle) / "vault.snapshot") != manifest["snapshot_index"]:
        raise ValueError("snapshot identity mismatch")
    return manifest


def backup(snapshot, custody, keyring, reference, output):
    for path in (snapshot, custody, reference):
        safe_input(path)
    private_directory(Path(output).parent)
    if Path(output).exists() or Path(output).is_symlink():
        raise ValueError("backup destination exists")
    # Validate the copied stable inputs; caller must use custody backup CLI, never
    # a live DB file copy. Concurrent source changes cannot yield a verified bundle.
    stage = Path(tempfile.mkdtemp(prefix=".backup-", dir=Path(output).parent))
    try:
        for source, name in ((snapshot, "vault.snapshot"), (custody, "custody.sqlite3"), (reference, "config-reference.json")):
            target = stage / name
            with open(source, "rb") as incoming, open(target, "xb") as outgoing:
                os.chmod(target, 0o600)
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        validate_reference(stage / "config-reference.json")
        manifest = {"version": 1, "files": {}, "custody_key_ids": custody_verify(stage / "custody.sqlite3", keyring), "snapshot_index": snapshot_verify(stage / "vault.snapshot")}
        for name in ("vault.snapshot", "custody.sqlite3", "config-reference.json"):
            with open(stage / name, "rb") as stream:
                manifest["files"][name] = hashlib.file_digest(stream, "sha256").hexdigest()
        exclusive_write(stage / "manifest.json", canonical(manifest) + b"\n")
        # rename fails if destination is a populated directory; root-private parent
        # is the operator trust boundary, and callers must serialize destination use.
        os.rename(stage, output)
        fsync_directory(Path(output).parent)
        return {"backup_written": True, "custody_key_ids": manifest["custody_key_ids"]}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def restore(bundle, keyring, output):
    manifest = verify_bundle(bundle, keyring)
    private_directory(Path(output).parent)
    Path(output).mkdir(mode=0o700)
    try:
        for name in ("vault.snapshot", "custody.sqlite3", "config-reference.json", "manifest.json"):
            target = Path(output) / name
            with open(Path(bundle) / name, "rb") as incoming, open(target, "xb") as outgoing:
                os.chmod(target, 0o600)
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        verify_bundle(output, keyring)
        fsync_directory(output)
        fsync_directory(Path(output).parent)
    except BaseException:
        # Keep incomplete new-only target for diagnosis. Never touch the source.
        raise
    return {"restore_inputs_verified": True, "vault_restore_and_unseal_required": True, "custody_key_ids": manifest["custody_key_ids"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "verify", "restore"))
    for key in ("snapshot", "custody", "reference", "bundle", "output"):
        parser.add_argument("--" + key)
    parser.add_argument("--keyring", required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("root required")
        os.umask(0o077)
        if args.command == "backup":
            result = backup(args.snapshot, args.custody, args.keyring, args.reference, args.output)
        elif args.command == "restore":
            result = restore(args.bundle, args.keyring, args.output)
        else:
            result = {"verified": True, "custody_key_ids": verify_bundle(args.bundle, args.keyring)["custody_key_ids"]}
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception:
        print('{"error":"CONTROL_BACKUP_FAILED"}', file=__import__('sys').stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
