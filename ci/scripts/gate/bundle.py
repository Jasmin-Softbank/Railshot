#!/usr/bin/env python3
"""Transfer tested images, never rebuild them. Requires a trusted CI artifact channel.

Hashes detect corruption; this manifest is not a signature or permission to publish.
Run isolated from concurrent source/tag writers; Docker credentials stay outside bundles.
export WORKSPACE VERDICT OUTDIR; publish BUNDLE REGISTRY_PREFIX TAG [--output PATH]
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import fcntl
import tarfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution import GATE_ORDER
from observability import OperationError, event_record
from process import run_bounded
from storage import durable_write
from runner.runtime_boundary import private_directory

HEX = r"[0-9a-f]{64}"
ID = rf"sha256:{HEX}"
REPO = r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
FILES = {"jasmin.yaml", "verdict.json", "images.tar"}
TRUST = "trusted-ci-artifact-not-a-signature"
LAYERS = list(GATE_ORDER)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_hash(path):
    require(not path.is_symlink() and path.is_file(), "bundle files must be regular files")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_digest(workspace):
    """64 hex chars over paths/type/permission mode/bytes, excluding every .git entry."""
    root = Path(workspace)
    require(not root.is_symlink() and root.is_dir(), "workspace must be a real directory")
    digest = hashlib.sha256(b"railshot-source-v1\0")
    def visit(directory):
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            if path.name == ".git":
                continue
            before = path.lstat()
            require(stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode), "source symlinks/special files are forbidden")
            is_file = stat.S_ISREG(before.st_mode)
            header = json.dumps([path.relative_to(root).as_posix(), "f" if is_file else "d",
                                 stat.S_IMODE(before.st_mode), before.st_size if is_file else 0], separators=(",", ":")).encode()
            digest.update(len(header).to_bytes(8, "big") + header)
            if is_file:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                    require(os.fstat(stream.fileno()) == before, "source changed while hashing")
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                after = path.lstat()
                require((before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) ==
                        (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode), "source changed while hashing")
            else:
                visit(path)
    visit(root)
    return digest.hexdigest()


def docker(*args, timeout=900):
    result = run_bounded(["docker", *args], timeout=timeout)
    require(result.returncode == 0, "Docker operation failed: " + " ".join(args[:2]))
    return result.stdout


def inspect(ref):
    data = json.loads(docker("image", "inspect", "--format", "{{json .}}", ref, timeout=30))
    require(isinstance(data, dict) and re.fullmatch(ID, data.get("Id", "")), "invalid Docker image ID")
    return data


def contract(spec_bytes, verdict_bytes):
    import jsonschema
    import yaml
    spec, verdict = yaml.safe_load(spec_bytes), json.loads(verdict_bytes)
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/jasmin.schema.json").read_text())
    jsonschema.validate(spec, schema)
    require(isinstance(verdict, dict) and verdict.get("release_eligible") is True and
            verdict.get("ok") is True and verdict.get("status") == "PASS", "full release verdict required")
    layers = verdict.get("layers", [])
    require([row.get("layer") for row in layers] == LAYERS and
            all(row.get("ok") is True and not row.get("blocked") and not row.get("errors") for row in layers),
            "all release layers must pass")
    require(isinstance(verdict.get("source_sha256"), str) and re.fullmatch(HEX, verdict["source_sha256"]), "source digest required")
    services = [service["name"] for service in spec["services"]]
    for service in spec["services"]:
        for path in (service["build"].get("context", "."), service["build"]["dockerfile"]):
            require(path and "\\" not in path and not PurePosixPath(path).is_absolute() and
                    ".." not in PurePosixPath(path).parts, "build paths must remain within workspace")
    images = verdict.get("images")
    require(len(services) == len(set(services)) and isinstance(images, dict) and set(images) == set(services), "service/image mismatch")
    require(all(isinstance(ref, str) and re.fullmatch(rf"{REPO}:{TAG}", ref) for ref in images.values()), "invalid local image reference")
    ids = verdict.get("image_ids")
    require(isinstance(ids, dict) and set(ids) == set(services) and
            all(isinstance(value, str) and re.fullmatch(ID, value) for value in ids.values()), "gate image IDs required")
    return verdict


def export(workspace, verdict_path, outdir):
    workspace, verdict_path, outdir = Path(workspace), Path(verdict_path), Path(outdir)
    require(not outdir.resolve().is_relative_to(workspace.resolve()), "bundle output must be outside workspace")
    require(not outdir.exists() and not outdir.is_symlink(), "bundle output must not exist")
    source = source_digest(workspace)
    spec_bytes = (workspace / ".jasmin/jasmin.yaml").read_bytes()
    require(not verdict_path.is_symlink() and verdict_path.is_file(), "verdict must be a regular file")
    verdict_bytes = verdict_path.read_bytes()
    verdict = contract(spec_bytes, verdict_bytes)
    require(verdict["source_sha256"] == source, "source changed since gate")
    images = {svc: {"local_ref": ref, "id": inspect(ref)["Id"]} for svc, ref in verdict["images"].items()}
    require(verdict["image_ids"] == {svc: item["id"] for svc, item in images.items()}, "image changed since gate")
    outdir.mkdir(parents=True)
    (outdir / "jasmin.yaml").write_bytes(spec_bytes)
    (outdir / "verdict.json").write_bytes(verdict_bytes)
    docker("image", "save", "--output", str((outdir / "images.tar").resolve()), *sorted({item["id"] for item in images.values()}))
    require(source_digest(workspace) == source, "source changed during export")
    manifest = {"version": 1, "trust": TRUST, "source_sha256": source, "images": images,
                "files": {name: file_hash(outdir / name) for name in sorted(FILES)}}
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify(bundle):
    bundle = Path(bundle)
    require(not bundle.is_symlink() and bundle.is_dir(), "bundle must be a real directory")
    require({p.name for p in bundle.iterdir()} == FILES | {"manifest.json"}, "unexpected bundle contents")
    file_hash(bundle / "manifest.json")
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    require(manifest.get("version") == 1 and manifest.get("trust") == TRUST, "unsupported bundle contract")
    require(set(manifest.get("files", {})) == FILES, "missing bundle hashes")
    for name in FILES:
        require(file_hash(bundle / name) == manifest["files"][name], "bundle hash mismatch: " + name)
    verdict = contract((bundle / "jasmin.yaml").read_bytes(), (bundle / "verdict.json").read_bytes())
    require(manifest.get("source_sha256") == verdict["source_sha256"], "manifest/verdict source mismatch")
    images = manifest.get("images", {})
    require(set(images) == set(verdict["images"]), "manifest service/image mismatch")
    for svc, item in images.items():
        require(set(item) == {"local_ref", "id"} and item["local_ref"] == verdict["images"][svc] and
                isinstance(item["id"], str) and re.fullmatch(ID, item["id"]), "manifest image mismatch")
    require(verdict["image_ids"] == {svc: item["id"] for svc, item in images.items()}, "manifest/gate image ID mismatch")
    return manifest


def skopeo(*args, authfile=None, timeout=900, diagnostics_dir=None):
    """Daemonless trusted publisher. Credential bytes never enter argv or output."""
    auth = []
    if authfile is not None:
        path = Path(authfile)
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                "publisher auth file must be a private owned regular file")
        auth = ["--authfile", str(path.resolve())]
    result = run_bounded(["skopeo", args[0], *auth, *args[1:]], timeout=timeout, raw=True)
    if result.returncode and diagnostics_dir is not None:
        # Private diagnostic only. Never persist argv, auth input, stdout or environment.
        raw = result.stderr if isinstance(result.stderr, bytes) else result.stderr.encode()
        detail = raw[-8192:].decode('utf-8', errors='replace')
        detail = re.sub(r'https?://[^\s"\']+', lambda m: m[0].split('?', 1)[0].split('#', 1)[0]
                        if '@' not in m[0] else '[REDACTED_URL]', detail)
        detail = re.sub(r'(?i)(authorization|password|token|secret|credential)\s*[:=]\s*[^\r\n]+',
                        r'\1=[REDACTED]', detail)
        detail = re.sub(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b', '[REDACTED_AWS_KEY]', detail)
        record = {'schema_version': 1, 'command': 'skopeo.' + args[0], 'exit_code': result.returncode,
                  'stderr_sha256': hashlib.sha256(raw).hexdigest(), 'stderr_bytes': len(raw),
                  'stderr_redacted': detail, 'truncated': len(raw) > 8192}
        durable_write(Path(diagnostics_dir) / ('native-failure-' + str(uuid.uuid4()) + '.json'),
                      json.dumps(record, sort_keys=True).encode())
    require(result.returncode == 0, "Skopeo operation failed")
    return result.stdout


def skopeo_manifest(ref, expected_id, *, authfile=None, diagnostics_dir=None):
    raw = skopeo("inspect", "--raw", ref, authfile=authfile, timeout=120, diagnostics_dir=diagnostics_dir)
    document = json.loads(raw)
    require(document.get("schemaVersion") == 2 and document.get("config", {}).get("digest") == expected_id,
            "registry/archive configuration differs from tested image ID")
    require(isinstance(document.get("layers"), list) and document["layers"], "single image manifest required")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def skopeo_archive(archive, image_id):
    """Bind a single-image archive to either classic config ID or Docker 29 OCI ID.

    OCI manifests/layers stay unchanged. Multi-image archives need an explicit
    projection implementation; do not silently choose the host's architecture.
    """
    with tarfile.open(archive, 'r:') as tar:
        def read(name):
            matches = [m for m in tar.getmembers() if m.name == name]
            require(len(matches) == 1 and matches[0].isfile() and matches[0].size <= 2 * 1024 * 1024,
                    'invalid archive identity member')
            return tar.extractfile(matches[0]).read()

        def blob(digest):
            require(isinstance(digest, str) and re.fullmatch(ID, digest), 'invalid archive digest')
            raw = read('blobs/sha256/' + digest.split(':', 1)[1])
            require('sha256:' + hashlib.sha256(raw).hexdigest() == digest, 'archive blob digest mismatch')
            return raw

        if 'index.json' in tar.getnames():
            index = json.loads(read('index.json'))
            require(index.get('schemaVersion') == 2 and len(index.get('manifests', [])) == 1,
                    'skopeo publisher supports one image per bundle')
            require(index['manifests'][0]['digest'] == image_id, 'archive index differs from gate image ID')
            native = json.loads(blob(image_id))
            config_id = native['config']['digest']
            config = json.loads(blob(config_id))
            require(config.get('os') == 'linux' and config.get('architecture') == 'amd64', 'unsupported image platform')
            return f'oci-archive:{archive}', config_id, image_id
        entries = json.loads(read('manifest.json'))
        require(isinstance(entries, list) and len(entries) == 1, 'skopeo publisher supports one image per bundle')
        config = read(entries[0]['Config'])
        require('sha256:' + hashlib.sha256(config).hexdigest() == image_id, 'archive config differs from gate image ID')
        return f'docker-archive:{archive}:@0', image_id, None


def publish(bundle, registry_prefix, tag, *, journal_dir=None, reconcile=False, backend="docker", authfile=None):
    require(isinstance(registry_prefix, str) and len(registry_prefix) <= 220 and
            re.fullmatch(REPO, registry_prefix) and "/" in registry_prefix and
            ("." in registry_prefix.split("/")[0] or ":" in registry_prefix.split("/")[0] or registry_prefix.startswith("localhost/")),
            "explicit registry/repository prefix required")
    require(isinstance(tag, str) and re.fullmatch(TAG, tag), "invalid image tag")
    require(backend in {"docker", "skopeo"}, "unsupported trusted publisher")
    require(backend == "skopeo" or authfile is None, "explicit authfile only supported by skopeo")
    manifest = verify(bundle)
    directory = private_directory(journal_dir or Path(bundle).with_name(Path(bundle).name + "-publish"))
    archive = str((Path(bundle) / "images.tar").resolve())
    if backend == "docker":
        docker("image", "load", "--input", archive)
        for item in manifest["images"].values():
            require(inspect(item["id"])["Id"] == item["id"], "loaded image ID mismatch")
    else:
        require(":" not in archive, "archive path cannot contain a transport separator")
        require(len(manifest['images']) == 1, 'skopeo publisher supports one image per bundle')
        for item in manifest["images"].values():
            archive_ref, config_id, native_manifest_id = skopeo_archive(archive, item['id'])
            inspected = skopeo_manifest(archive_ref, config_id, diagnostics_dir=directory)
            require(native_manifest_id is None or inspected == native_manifest_id, 'OCI archive manifest differs')
    lock = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = directory / "publish.json"
        binding = {"manifest_sha256": file_hash(Path(bundle) / "manifest.json"),
                   "registry_prefix": registry_prefix, "tag": tag}
        if backend != "docker":
            binding["backend"] = backend
        state = json.loads(journal.read_bytes()) if journal.exists() else {"binding": binding, "images": {}}
        require(state["binding"] == binding, "publish journal binding differs")
        for svc, item in manifest["images"].items():
            repository = f"{registry_prefix}-{svc}"
            target = f"{repository}:{tag}"
            previous = state["images"].get(svc, {})
            if previous.get("outcome") == "PASS":
                continue
            uncertain = bool(previous)
            if uncertain and not reconcile:
                raise OperationError("PUBLISH_OUTCOME_UNKNOWN", component="publish", phase="resume",
                                     outcome="UNKNOWN", retry_policy="after_reconcile", side_effect="unknown")
            state["images"][svc] = {"target": target, "image_id": item["id"], "outcome": "UNKNOWN"}
            durable_write(journal, (json.dumps(state, indent=2) + "\n").encode())
            try:
                if backend == "skopeo":
                    if not uncertain:
                        skopeo("copy", *(["--preserve-digests"] if native_manifest_id else []), archive_ref,
                               "docker://" + target, authfile=authfile, diagnostics_dir=directory)
                    # Inspect the remote manifest, then inspect its immutable reference.
                    remote_digest = skopeo_manifest("docker://" + target, config_id, authfile=authfile, diagnostics_dir=directory)
                    require(native_manifest_id is None or remote_digest == native_manifest_id, 'published OCI identity changed')
                    reference = repository + "@" + remote_digest
                    require(skopeo_manifest("docker://" + reference, config_id, authfile=authfile, diagnostics_dir=directory) == remote_digest,
                            "immutable registry readback differs")
                    state["images"][svc].update(outcome="PASS", digest=reference)
                    durable_write(journal, (json.dumps(state, indent=2) + "\n").encode())
                    continue
                if uncertain:
                    # Read back the remote tag before deciding; never re-push an uncertain attempt.
                    docker("image", "pull", target)
                else:
                    docker("image", "tag", item["id"], target, timeout=30)
                    require(inspect(target)["Id"] == item["id"], "tagged image ID mismatch")
                    docker("image", "push", target)
                data = inspect(target)
                require(data["Id"] == item["id"], "published image ID mismatch")
                digests = {ref for ref in data.get("RepoDigests", []) if isinstance(ref, str)
                           and re.fullmatch(re.escape(repository) + "@" + ID, ref)}
                require(len(digests) == 1, "exact target repository digest required")
                state["images"][svc].update(outcome="PASS", digest=digests.pop())
                durable_write(journal, (json.dumps(state, indent=2) + "\n").encode())
            except Exception as exc:
                state['images'][svc]['diagnostics'] = [
                    {'path': p.name, 'sha256': file_hash(p)} for p in sorted(directory.glob('native-failure-*.json'))]
                durable_write(journal, (json.dumps(state, indent=2) + "\n").encode())
                raise OperationError("PUBLISH_OUTCOME_UNKNOWN", component="publish", phase="image",
                                     outcome="UNKNOWN", retry_policy="after_reconcile", side_effect="possible", cause=exc) from exc
        return {svc: entry["digest"] for svc, entry in state["images"].items()}
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exp = commands.add_parser("export")
    for field in ("workspace", "verdict", "outdir"):
        exp.add_argument(field, type=Path)
    pub = commands.add_parser("publish")
    pub.add_argument("bundle", type=Path)
    pub.add_argument("registry_prefix")
    pub.add_argument("tag")
    pub.add_argument("--output", type=Path)
    pub.add_argument("--journal-dir", type=Path)
    pub.add_argument("--reconcile", action="store_true", help="read back uncertain remote tags; never re-push them")
    pub.add_argument("--backend", choices=("docker", "skopeo"), default="docker")
    pub.add_argument("--authfile", type=Path, help="private publisher-owned skopeo authfile; never a CI input")
    args = parser.parse_args()
    try:
        if args.command == "publish" and args.output:
            require(not args.output.exists() and not args.output.is_symlink() and args.output.parent.is_dir(), "output receipt path must be new with an existing parent")
        result = export(args.workspace, args.verdict, args.outdir) if args.command == "export" else publish(
            args.bundle, args.registry_prefix, args.tag, journal_dir=args.journal_dir, reconcile=args.reconcile,
            backend=args.backend, authfile=args.authfile)
        encoded = json.dumps(result, indent=2) + "\n"
        if args.command == "publish" and args.output:
            durable_write(args.output, encoded.encode())
        print(encoded, end="")
        return 0
    except Exception as exc:
        error = exc if isinstance(exc, OperationError) else OperationError(
            "GATE_CONFIG_INVALID" if isinstance(exc, ValueError) else "OBSERVATION_WRITE_FAILED",
            component="publish", phase=args.command, retry_policy="after_reconcile", cause=exc)
        print(json.dumps(event_record("publish.failed", component="publish", phase=args.command,
                                      outcome=error.outcome, error=error)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
