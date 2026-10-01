import json
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch

import bundle


IMAGE = "sha256:" + "a" * 64
DIGEST = "sha256:" + "b" * 64
LOCAL = "railshot-gate/demo-web:run123"


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ws = self.root / "workspace"
        (self.ws / ".jasmin").mkdir(parents=True)
        (self.ws / ".jasmin/jasmin.yaml").write_text(
            "apiVersion: jasmin/v0\napp: demo\nservices:\n"
            "  - name: web\n    build: {dockerfile: Dockerfile}\n    port: 3000\n    route: /\n")
        (self.ws / "Dockerfile").write_text("FROM node:22\nUSER 10001\n")
        (self.ws / "untracked.py").write_text("print('source')\n")
        self.verdict_path = self.root / "verdict.json"
        self.out = self.root / "bundle"
        self.verdict = {"ok": True, "release_eligible": True, "status": "PASS",
                        "layers": [{"layer": layer, "ok": True} for layer in bundle.LAYERS],
                        "source_sha256": bundle.source_digest(self.ws), "images": {"web": LOCAL},
                        "image_ids": {"web": IMAGE}}
        self.write_verdict()
        self.commands = []
        self.tag_ids = {LOCAL: IMAGE, IMAGE: IMAGE}
        self.wrong_digest = False

    def write_verdict(self):
        self.verdict_path.write_text(json.dumps(self.verdict))

    def docker_run(self, args, **kwargs):
        self.commands.append(args)
        self.assertEqual("docker", args[0])
        self.assertIn("timeout", kwargs)
        self.assertNotIn("shell", kwargs)
        operation = args[2]
        output = ""
        if operation == "inspect":
            ref = args[-1]
            repository = "wrong.example/image" if self.wrong_digest else ref.rsplit(":", 1)[0]
            output = json.dumps({"Id": self.tag_ids.get(ref, "sha256:" + "c" * 64),
                                 "RepoDigests": [repository + "@" + DIGEST]})
        elif operation == "save":
            self.assertEqual([IMAGE], args[5:])
            Path(args[4]).write_bytes(b"mock immutable docker image tar")
        elif operation == "tag":
            self.tag_ids[args[4]] = self.tag_ids[args[3]]
        elif operation not in {"load", "push", "pull"}:
            self.fail("unexpected Docker operation: " + operation)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    def export(self):
        with patch("bundle.run_bounded", side_effect=self.docker_run):
            return bundle.export(self.ws, self.verdict_path, self.out)

    def test_skopeo_publishes_exact_config_without_docker_daemon(self):
        self.export()
        raw = json.dumps({'schemaVersion': 2, 'config': {'digest': IMAGE},
                          'layers': [{'digest': DIGEST}]}).encode()
        with patch('bundle.skopeo_archive', return_value=('docker-archive:synthetic:@0', IMAGE, None)), \
                patch('bundle.skopeo', return_value=raw) as copy, patch('bundle.docker') as docker:
            result = bundle.publish(self.out, 'registry.example/demo', 'r1', backend='skopeo')
        docker.assert_not_called()
        self.assertEqual(1, sum(call.args[0] == 'copy' for call in copy.call_args_list))
        self.assertTrue(result['web'].startswith('registry.example/demo-web@sha256:'))
        self.assertIn('docker://' + result['web'], [call.args[-1] for call in copy.call_args_list])

    def test_skopeo_failure_has_private_bounded_redacted_native_diagnostic(self):
        directory = self.root / 'diagnostics'; directory.mkdir(mode=0o700)
        raw = b'copy failed: unsupported image format\nAuthorization: Bearer synthetic-secret\nhttps://storage.example/blob?X-Amz-Security-Token=synthetic-token\n'
        result = subprocess.CompletedProcess([], 17, b'never persist stdout', raw)
        with patch('bundle.run_bounded', return_value=result), self.assertRaisesRegex(ValueError, '^Skopeo operation failed$'):
            bundle.skopeo('copy', 'local', 'remote', diagnostics_dir=directory)
        paths = list(directory.glob('native-failure-*.json')); self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_bytes())
        self.assertEqual(record['exit_code'], 17)
        self.assertEqual(record['stderr_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertIn('unsupported image format', record['stderr_redacted'])
        self.assertNotIn('synthetic-secret', paths[0].read_text())
        self.assertNotIn('synthetic-token', paths[0].read_text())
        self.assertNotIn('never persist stdout', paths[0].read_text())
        self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)

    def test_skopeo_config_mismatch_fails_before_copy(self):
        self.export()
        raw = json.dumps({'schemaVersion': 2, 'config': {'digest': 'sha256:' + 'c' * 64},
                          'layers': [{'digest': DIGEST}]}).encode()
        with patch('bundle.skopeo_archive', return_value=('docker-archive:synthetic:@0', IMAGE, None)), \
                patch('bundle.skopeo', return_value=raw) as copy, self.assertRaises(ValueError):
            bundle.publish(self.out, 'registry.example/demo', 'r1', backend='skopeo')
        self.assertFalse(any(call.args[0] == 'copy' for call in copy.call_args_list))

    def test_skopeo_uncertain_copy_is_not_retried(self):
        self.export()
        raw = json.dumps({'schemaVersion': 2, 'config': {'digest': IMAGE},
                          'layers': [{'digest': DIGEST}]}).encode()
        def fail_copy(*args, **kwargs):
            if args[0] == 'copy':
                raise OSError('synthetic failure')
            return raw
        with patch('bundle.skopeo_archive', return_value=('docker-archive:synthetic:@0', IMAGE, None)), \
                patch('bundle.skopeo', side_effect=fail_copy), self.assertRaises(bundle.OperationError):
            bundle.publish(self.out, 'registry.example/demo', 'r1', backend='skopeo')
        with patch('bundle.skopeo_archive', return_value=('docker-archive:synthetic:@0', IMAGE, None)), \
                patch('bundle.skopeo', return_value=raw) as copy:
            with self.assertRaises(bundle.OperationError):
                bundle.publish(self.out, 'registry.example/demo', 'r1', backend='skopeo')
            result = bundle.publish(self.out, 'registry.example/demo', 'r1', backend='skopeo', reconcile=True)
        self.assertFalse(any(call.args[0] == 'copy' for call in copy.call_args_list))
        self.assertIn('web', result)

    def test_actual_oci_archive_manifest_id_is_not_confused_with_config_id(self):
        config = json.dumps({'os': 'linux', 'architecture': 'amd64'}).encode()
        config_id = 'sha256:' + hashlib.sha256(config).hexdigest()
        manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': config_id}, 'layers': []}).encode()
        image_id = 'sha256:' + hashlib.sha256(manifest).hexdigest()
        archive = self.root / 'oci.tar'
        def write_archive(config_data, duplicates=False):
            files = {'index.json': json.dumps({'schemaVersion': 2, 'manifests': [{'digest': image_id}]}).encode(),
                     'blobs/sha256/' + image_id.split(':')[1]: manifest,
                     'blobs/sha256/' + config_id.split(':')[1]: config_data}
            with tarfile.open(archive, 'w:') as tar:
                for name, data in files.items():
                    item = tarfile.TarInfo(name); item.size = len(data); tar.addfile(item, io.BytesIO(data))
                if duplicates:
                    item = tarfile.TarInfo('index.json'); item.size = len(files['index.json']); tar.addfile(item, io.BytesIO(files['index.json']))
        write_archive(config)
        self.assertEqual((f'oci-archive:{archive}', config_id, image_id), bundle.skopeo_archive(str(archive), image_id))
        write_archive(config + b'tamper')
        with self.assertRaisesRegex(ValueError, 'blob digest mismatch'):
            bundle.skopeo_archive(str(archive), image_id)
        write_archive(config, duplicates=True)
        with self.assertRaisesRegex(ValueError, 'identity member'):
            bundle.skopeo_archive(str(archive), image_id)

    def test_digest_includes_untracked_hidden_files_and_mode_but_not_git(self):
        original = bundle.source_digest(self.ws)
        (self.ws / ".git").mkdir()
        (self.ws / ".git/index").write_bytes(b"metadata")
        self.assertEqual(original, bundle.source_digest(self.ws))
        source = self.ws / "untracked.py"
        source.write_text("changed")
        self.assertNotEqual(original, bundle.source_digest(self.ws))
        before_mode = bundle.source_digest(self.ws)
        source.chmod(source.stat().st_mode ^ 0o100)
        self.assertNotEqual(before_mode, bundle.source_digest(self.ws))
        before_spec = bundle.source_digest(self.ws)
        (self.ws / ".jasmin/jasmin.yaml").write_text("changed")
        self.assertNotEqual(before_spec, bundle.source_digest(self.ws))

    def test_source_symlinks_are_rejected(self):
        (self.ws / "link").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            bundle.source_digest(self.ws)

    def test_changed_source_fails_before_export_or_docker(self):
        (self.ws / "Dockerfile").write_text("changed")
        with patch("bundle.run_bounded") as run:
            with self.assertRaisesRegex(ValueError, "source changed"):
                bundle.export(self.ws, self.verdict_path, self.out)
            run.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_partial_gate_and_service_mismatch_are_not_releasable(self):
        for change in ({"release_eligible": False}, {"layers": self.verdict["layers"][:-1]},
                       {"images": {"other": LOCAL}}, {"image_ids": {}}):
            with self.subTest(change=change):
                verdict = {**self.verdict, **change}
                with self.assertRaises(ValueError):
                    bundle.contract((self.ws / ".jasmin/jasmin.yaml").read_bytes(), json.dumps(verdict))

    def test_export_has_only_spec_verdict_tar_and_manifest_and_immutable_ids(self):
        manifest = self.export()
        self.assertEqual(bundle.FILES | {"manifest.json"}, {p.name for p in self.out.iterdir()})
        self.assertEqual(IMAGE, manifest["images"]["web"]["id"])
        self.assertEqual(bundle.TRUST, manifest["trust"])
        self.assertEqual(manifest, bundle.verify(self.out))

    def test_gate_image_retag_is_rejected(self):
        self.tag_ids[LOCAL] = "sha256:" + "c" * 64
        with self.assertRaisesRegex(ValueError, "image changed"):
            self.export()
        self.assertFalse(self.out.exists())

    def test_payload_tamper_and_source_manifest_tamper_fail_before_load(self):
        self.export()
        tar = self.out / "images.tar"
        original = tar.read_bytes()
        tar.write_bytes(b"tampered")
        with patch("bundle.run_bounded") as run:
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                bundle.publish(self.out, "ghcr.io/owner/tenant-app", "v1")
            run.assert_not_called()
        tar.write_bytes(original)
        manifest_path = self.out / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["source_sha256"] = "d" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "source mismatch"):
            bundle.verify(self.out)

    def test_publish_pushes_loaded_id_without_any_build(self):
        self.export()
        self.commands.clear()
        with patch("bundle.run_bounded", side_effect=self.docker_run):
            published = bundle.publish(self.out, "ghcr.io/owner/tenant-app", "v1")
        self.assertEqual({"web": "ghcr.io/owner/tenant-app-web@" + DIGEST}, published)
        operations = [command[2] for command in self.commands]
        self.assertEqual("load", operations[0])
        self.assertIn(["docker", "image", "tag", IMAGE, "ghcr.io/owner/tenant-app-web:v1"], self.commands)
        self.assertNotIn("build", operations)
        self.assertEqual(1, operations.count("push"))

    def test_loaded_id_mismatch_stops_before_tag_or_push(self):
        self.export()
        self.commands.clear()
        self.tag_ids[IMAGE] = "sha256:" + "c" * 64
        with patch("bundle.run_bounded", side_effect=self.docker_run):
            with self.assertRaisesRegex(ValueError, "loaded image"):
                bundle.publish(self.out, "ghcr.io/owner/tenant-app", "v1")
        self.assertNotIn("push", [command[2] for command in self.commands])
        self.assertNotIn("tag", [command[2] for command in self.commands])

    def test_wrong_registry_digest_is_not_a_published_result(self):
        self.export()
        self.wrong_digest = True
        with patch("bundle.run_bounded", side_effect=self.docker_run):
            with self.assertRaises(bundle.OperationError):
                bundle.publish(self.out, "ghcr.io/owner/tenant-app", "v1")

    def test_paths_and_registry_inputs_do_not_become_options_or_commands(self):
        with self.assertRaisesRegex(ValueError, "outside workspace"):
            bundle.export(self.ws, self.verdict_path, self.ws / "output")
        for prefix, tag in (("--help", "v1"), ("ghcr.io/owner/app;echo", "v1"), ("ghcr.io/owner/app", "$(echo bad)")):
            with patch("bundle.run_bounded") as run:
                with self.assertRaises(ValueError):
                    bundle.publish(self.out, prefix, tag)
                run.assert_not_called()

    def test_partial_publish_is_durable_and_uncertain_push_requires_readback(self):
        spec = self.ws / ".jasmin/jasmin.yaml"
        spec.write_text(spec.read_text() + "  - name: api\n    build: {dockerfile: Dockerfile}\n    port: 3001\n")
        self.verdict["images"]["api"] = LOCAL
        self.verdict["image_ids"]["api"] = IMAGE
        self.verdict["source_sha256"] = bundle.source_digest(self.ws)
        self.write_verdict()
        self.export()
        pushes = []
        def fail_second(args, **kwargs):
            if args[2] == "push":
                pushes.append(args[-1])
                if len(pushes) == 2:
                    return subprocess.CompletedProcess(args, 125, "", "synthetic transport error")
            return self.docker_run(args, **kwargs)
        with patch("bundle.run_bounded", side_effect=fail_second), self.assertRaises(bundle.OperationError):
            bundle.publish(self.out, "ghcr.io/owner/tenant-app", "v1")
        journal = json.loads((self.root / "bundle-publish/publish.json").read_text())
        self.assertEqual([v["outcome"] for v in journal["images"].values()], ["PASS", "UNKNOWN"])
        self.commands.clear()
        with patch("bundle.run_bounded", side_effect=self.docker_run):
            with self.assertRaises(bundle.OperationError):
                bundle.publish(self.out, "ghcr.io/owner/tenant-app", "v1")
            result = bundle.publish(self.out, "ghcr.io/owner/tenant-app", "v1", reconcile=True)
        self.assertEqual(set(result), {"web", "api"})
        operations = [cmd[2] for cmd in self.commands]
        self.assertEqual(operations.count("pull"), 1)
        self.assertNotIn("push", operations)

    def test_build_context_outside_source_digest_is_rejected(self):
        spec = (self.ws / ".jasmin/jasmin.yaml").read_text()
        for path in ("../outside", "/tmp/outside", "nested/../../outside"):
            with self.assertRaisesRegex(ValueError, "within workspace"):
                bundle.contract(spec.replace("{dockerfile: Dockerfile}", "{dockerfile: Dockerfile, context: " + path + "}"),
                                json.dumps(self.verdict))


if __name__ == "__main__":
    unittest.main()
