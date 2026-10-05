import base64
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("secrets_runtime", ROOT / "deployment/scripts/secrets_runtime.py")
runtime = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(runtime)


def profile(provider="aws"):
    seal = {"aws": "awskms", "gcp": "gcpckms", "openstack": "transit", "onprem": "transit"}[provider]
    return {"version": 1, "environment_id": "env-1", "provider_profile": {
        "provider": provider, "namespace": "railshot-secrets",
        "storage": {"class_name": "vault-retain", "capacity": "10Gi", "manifest_sha256": "c" * 64},
        "vault": {"image": "hashicorp/vault@sha256:" + "a" * 64, "tls_secret": "vault-tls",
                  "seal_secret": "vault-seal", "seal": {"type": seal},
                  "tls": {"cert_file": "/staged/tls.crt", "key_file": "/staged/tls.key", "ca_file": "/staged/ca.crt"},
                  "seal_env_file": "/staged/seal.json"},
        "external_secrets": {"manifest_sha256": "b" * 64},
        "recovery": {"helper": "/usr/local/libexec/railshot-recovery-escrow"}}}


class FakeRunner:
    def __init__(self):
        self.initialized = False; self.marker = None; self.readiness = None
        self.escrows = []; self.documents = []; self.root_revoked = False; self.vault_calls = []
        self.fail_status_count = 0
        self.materials = {}; self.init_count = 0; self.fail_after_revoke = False; self.fail_after_custody = False

    def command(self, argv, **kwargs):
        joined = " ".join(argv)
        if "operator init" in joined:
            self.init_count += 1
            self.initialized = True
            return {"root_token": "root-sensitive", "recovery_keys_b64": ["key"] * 5}
        if "vault status" in joined:
            if self.fail_status_count:
                self.fail_status_count -= 1
                raise runtime.RuntimeErrorCode("SECRETS_COMMAND_FAILED", "fixture listener race")
            return '{"initialized":%s,"sealed":false}' % str(self.initialized).lower()
        return ""

    def custody(self, environment_id, kind, operation_id, *, action="lookup", material=None):
        key = (environment_id, kind, operation_id)
        if action == "store":
            self.escrows.append((environment_id, kind, material)); self.materials[key] = material
            if self.fail_after_custody:
                self.fail_after_custody = False
                raise runtime.RuntimeErrorCode("RECOVERY_ESCROW_UNKNOWN", "response lost", unknown=True)
        if key not in self.materials: return None
        return {"stored": True, "receipt_id": "receipt-" + kind, **({"material": self.materials[key]} if action == "recover" else {})}

    def token_valid(self, token):
        return not self.root_revoked

    def vault(self, token, args, payload=None, **kwargs):
        self.vault_calls.append((list(args), payload))
        if args[:3] == ["secrets", "list", "-format=json"]: return {}
        if args[:3] == ["auth", "list", "-format=json"]: return {}
        if args[-1:] == ["auth/approle/role/railshot-delivery/role-id"]: return {"data": {"role_id": "role-sensitive"}}
        if args[-1:] == ["auth/approle/role/railshot-delivery/secret-id"]: return {"data": {"secret_id": "secret-sensitive"}}
        if args[:2] == ["token", "revoke"]:
            self.root_revoked = True
            if self.fail_after_revoke:
                self.fail_after_revoke = False
                raise runtime.RuntimeErrorCode("ROOT_UNKNOWN", "revocation response lost", unknown=True)
        if args[:3] == ["kv", "put", "-mount=railshot"]:
            self.readiness = args[-1].split("=", 1)[1]
        return ""

    def kube(self, *args, document=None, **kwargs):
        if document is not None:
            self.documents.append(document)
            if document.get("kind") == "ConfigMap" and document["metadata"]["name"] == "railshot-secrets-state":
                self.marker = document
            return document
        joined = " ".join(args)
        if "get storageclass" in joined: return {"reclaimPolicy": "Retain"}
        if "--ignore-not-found" in args and ("get secret vault-tls" in joined or "get secret vault-seal" in joined): return None
        if "get secret vault-tls" in joined: return {"data": {"tls.crt": "x", "tls.key": "x", "ca.crt": "x"}}
        if "get secret vault-seal" in joined: return {"data": {"AWS_REGION": "x"}}
        if "get configmap railshot-secrets-state" in joined: return self.marker
        if "get secret railshot-readiness" in joined:
            return None if self.readiness is None else {"data": {"value": base64.b64encode(self.readiness.encode()).decode()}}
        if "get pvc vault-data-vault-0" in joined:
            return {"spec": {"storageClassName": "vault-retain", "volumeName": "pv-1"}, "status": {"phase": "Bound"}}
        if "get statefulset vault" in joined:
            return {"status": {"readyReplicas": 1, "currentRevision": "r1", "updateRevision": "r1"}}
        if "get deployment external-secrets" in joined: return {"status": {"availableReplicas": 1}}
        return {}


class RuntimeTests(unittest.TestCase):
    def manifests(self, config):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        eso = root / "eso.yaml"; eso.write_bytes(b"pinned external secrets manifest")
        storage = root / "storage.yaml"; storage.write_bytes(b"pinned storage provider manifest")
        config["provider_profile"]["external_secrets"]["manifest_sha256"] = runtime.hashlib.sha256(eso.read_bytes()).hexdigest()
        config["provider_profile"]["storage"]["manifest_sha256"] = runtime.hashlib.sha256(storage.read_bytes()).hexdigest()
        for name, value, mode in (("tls.crt", "cert", 0o644), ("tls.key", "key", 0o600), ("ca.crt", "ca", 0o644),
                                  ("seal.json", '{"AWS_REGION":"ap-northeast-2","VAULT_AWSKMS_SEAL_KEY_ID":"key-1"}', 0o600)):
            path = root / name; path.write_text(value); path.chmod(mode)
        vault = config["provider_profile"]["vault"]
        vault["tls"] = {"cert_file": str(root / "tls.crt"), "key_file": str(root / "tls.key"), "ca_file": str(root / "ca.crt")}
        vault["seal_env_file"] = str(root / "seal.json")
        return eso, storage

    def test_configure_initializes_escrows_restarts_readbacks_and_revokes_root(self):
        config = profile(); eso, storage = self.manifests(config); runner = FakeRunner()
        checks = runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
        self.assertTrue(all(checks.values()))
        self.assertEqual([row[1] for row in runner.escrows], ["vault-initialization", "vault-delivery-approle"])
        self.assertTrue(runner.root_revoked)
        kubernetes_auth = next(args for args, _ in runner.vault_calls if args[:2] == ["write", "auth/kubernetes/config"])
        self.assertEqual(kubernetes_auth, ["write", "auth/kubernetes/config",
                                          "kubernetes_host=https://kubernetes.default.svc:443"])
        self.assertNotIn("token_reviewer_jwt", " ".join(kubernetes_auth))
        issuer = next(args for args, _ in runner.vault_calls if args[:2] == ["write", "auth/kubernetes/role/railshot-delivery-issuer"])
        self.assertIn("bound_service_account_names=vault", issuer)
        self.assertIn("bound_service_account_namespaces=railshot-secrets", issuer)
        self.assertIn("audience=vault", issuer); self.assertIn("token_max_ttl=10m", issuer)
        issuer_policy = next(body for args, body in runner.vault_calls if args[:3] == ["policy", "write", "railshot-delivery-issuer"])
        self.assertNotIn('*', issuer_policy); self.assertNotIn('sys/', issuer_policy)
        self.assertEqual(runner.marker["data"]["phase"], "ready")
        kinds = {doc["kind"] for doc in runner.documents}
        self.assertTrue({"Namespace", "PersistentVolumeClaim", "StatefulSet", "SecretStore", "ExternalSecret"} <= kinds)
        statefulset = next(doc for doc in runner.documents if doc["kind"] == "StatefulSet")
        container = statefulset["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["/bin/vault"])
        self.assertEqual(container["args"], ["server", "-config=/vault/config/vault.hcl"])
        self.assertEqual(statefulset["spec"]["template"]["spec"]["securityContext"]["runAsUser"], 100)
        self.assertIn("disable_mlock = true", next(doc for doc in runner.documents
            if doc["kind"] == "ConfigMap" and "vault.hcl" in doc.get("data", {}))["data"]["vault.hcl"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertIn({"name": "data", "persistentVolumeClaim": {"claimName": "vault-data-vault-0"}},
                      statefulset["spec"]["template"]["spec"]["volumes"])
        self.assertEqual(container["readinessProbe"]["tcpSocket"]["port"], "https")

    def test_transient_vault_listener_failure_is_reobserved_within_bound(self):
        config = profile(); eso, storage = self.manifests(config); runner = FakeRunner(); runner.fail_status_count = 1
        with patch.object(runtime.time, "sleep", return_value=None) as sleep:
            checks = runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
        self.assertTrue(checks["vault_unsealed"]); sleep.assert_called()

    def test_initialized_without_ready_marker_is_unknown_and_never_reinitialized(self):
        config = profile(); eso, storage = self.manifests(config); runner = FakeRunner(); runner.initialized = True
        with self.assertRaises(runtime.RuntimeErrorCode) as caught:
            runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
        self.assertEqual(caught.exception.code, "VAULT_INITIALIZATION_UNKNOWN")
        self.assertTrue(caught.exception.unknown)
        self.assertEqual(runner.escrows, [])

    def test_provider_specific_auto_unseal_and_retain_storage_are_required(self):
        config = profile("gcp")
        with self.assertRaises(runtime.RuntimeErrorCode):
            runtime.validate(config, "env-1", "aws")
        config = profile("aws"); runner = FakeRunner()
        runner.kube = lambda *args, **kwargs: ({"reclaimPolicy": "Delete"} if "storageclass" in args else {})
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "eso"; manifest.write_bytes(b"x")
            config["provider_profile"]["external_secrets"]["manifest_sha256"] = runtime.hashlib.sha256(b"x").hexdigest()
            with self.assertRaisesRegex(runtime.RuntimeErrorCode, "retain"):
                runtime.validate_prerequisites(runner, config["provider_profile"], manifest)

    def test_manifests_pin_images_tls_and_do_not_embed_recovery_material(self):
        docs = runtime.documents(profile()["provider_profile"], "env-1")
        rendered = runtime.json.dumps(docs)
        self.assertIn("@sha256:", rendered)
        self.assertIn("vault-tls", rendered)
        self.assertNotIn("root-sensitive", rendered)
        self.assertNotIn("recovery_keys", rendered)
        config = next(doc for doc in docs if doc["kind"] == "ConfigMap")["data"]["vault.hcl"]
        self.assertIn('cluster_addr = "https://vault.railshot-secrets.svc:8201"', config)
        self.assertIn('cluster_address = "0.0.0.0:8201"', config)
        service = next(doc for doc in docs if doc["kind"] == "Service")
        self.assertIn({"name": "cluster", "port": 8201, "targetPort": 8201}, service["spec"]["ports"])

    def test_custody_response_loss_and_root_revocation_loss_resume_without_reinitialization(self):
        for failure in ("fail_after_custody", "fail_after_revoke"):
            with self.subTest(failure=failure):
                config = profile(); eso, storage = self.manifests(config); runner = FakeRunner()
                setattr(runner, failure, True)
                with self.assertRaises(runtime.RuntimeErrorCode):
                    runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
                result = runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
                self.assertTrue(result["vault_unsealed"]); self.assertTrue(runner.root_revoked)
                self.assertEqual(runner.init_count, 1)
                self.assertEqual(len(runner.escrows), 2)
                self.assertNotIn("root-sensitive", runtime.json.dumps(runner.marker))

    def test_all_providers_can_select_distinct_transit_key_and_mounted_ca(self):
        for provider in ("aws", "gcp", "openstack", "onprem"):
            config = profile(provider)
            config["provider_profile"]["vault"]["seal"] = {"type": "transit", "address": "https://central.example.test:8200",
                "key_name": "railshot-env-1", "mount_path": "transit/", "ca_file": "/staged/transit-ca.crt"}
            value = runtime.validate(config, "env-1", provider)
            documents = runtime.documents(value, "env-1")
            hcl = next(doc for doc in documents if doc["kind"] == "ConfigMap")["data"]["vault.hcl"]
            self.assertIn('token = "env://VAULT_TOKEN"', hcl)
            self.assertIn('tls_ca_cert = "/vault/transit/ca.crt"', hcl)
            self.assertNotIn("VAULT_TRANSIT_SEAL_TOKEN", runtime.json.dumps(documents))
            config["provider_profile"]["vault"]["seal"]["key_name"] = "railshot-other"
            with self.assertRaises(runtime.RuntimeErrorCode): runtime.validate(config, "env-1", provider)

    def test_versioned_material_rotation_retains_old_secrets_and_rolls_back_owned_workload(self):
        config = profile(); eso, storage = self.manifests(config); runner = FakeRunner()
        runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
        first = next(doc for doc in runner.documents if doc["kind"] == "StatefulSet")
        first["metadata"]["generation"] = 1
        first["status"] = {"readyReplicas": 1, "currentRevision": "ready-1",
                           "updateRevision": "ready-1", "observedGeneration": 1}
        old_names = {doc["metadata"]["name"] for doc in runner.documents if doc["kind"] == "Secret"}
        Path(config["provider_profile"]["vault"]["tls"]["key_file"]).write_text("new-key")
        original = runner.kube
        def kube(*args, document=None, **kwargs):
            if document is None and "statefulset" in args and "--ignore-not-found" in args: return first
            return original(*args, document=document, **kwargs)
        runner.kube = kube
        with patch.object(runtime, "configure_vault", side_effect=runtime.RuntimeErrorCode("ROTATION_FAILED", "fixture")):
            with self.assertRaises(runtime.RuntimeErrorCode):
                runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
        last = [doc for doc in runner.documents if doc["kind"] == "StatefulSet"][-1]
        self.assertEqual(last["spec"], first["spec"])
        new_names = {doc["metadata"]["name"] for doc in runner.documents if doc["kind"] == "Secret"}
        self.assertTrue(old_names < new_names)

    def test_resume_does_not_roll_back_to_unready_or_unobserved_previous_workload(self):
        for status in (
            {"readyReplicas": 0, "currentRevision": "old", "updateRevision": "old", "observedGeneration": 2},
            {"readyReplicas": 1, "currentRevision": "old", "updateRevision": "new", "observedGeneration": 2},
            {"readyReplicas": 1, "currentRevision": "old", "updateRevision": "old", "observedGeneration": 1},
        ):
            with self.subTest(status=status):
                config = profile(); eso, storage = self.manifests(config); runner = FakeRunner()
                previous = runtime.documents(config["provider_profile"], "env-1")[-1]
                previous["metadata"]["generation"] = 2
                previous["status"] = status
                previous["spec"]["template"]["spec"]["containers"][0]["command"] = ["/broken-entrypoint"]
                original = runner.kube
                def kube(*args, document=None, **kwargs):
                    if document is None and "statefulset" in args and "--ignore-not-found" in args:
                        return previous
                    return original(*args, document=document, **kwargs)
                runner.kube = kube
                with patch.object(runtime, "configure_vault", side_effect=runtime.RuntimeErrorCode("START_FAILED", "fixture")):
                    with self.assertRaises(runtime.RuntimeErrorCode):
                        runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
                applied = [doc for doc in runner.documents if doc["kind"] == "StatefulSet"]
                self.assertEqual(len(applied), 1)
                self.assertEqual(applied[0]["spec"]["template"]["spec"]["containers"][0]["command"], ["/bin/vault"])

    def test_unknown_initialization_never_rolls_back_and_failed_rollback_preserves_cause(self):
        for unknown in (True, False):
            with self.subTest(unknown=unknown):
                config = profile(); eso, storage = self.manifests(config); runner = FakeRunner()
                previous = runtime.documents(config["provider_profile"], "env-1")[-1]
                previous["metadata"]["generation"] = 1
                previous["status"] = {"readyReplicas": 1, "currentRevision": "r1", "updateRevision": "r1", "observedGeneration": 1}
                original_kube = runner.kube; applied = []
                def kube(*args, document=None, **kwargs):
                    if document is None and "statefulset" in args and "--ignore-not-found" in args: return previous
                    if document and document.get("kind") == "StatefulSet":
                        applied.append(document)
                        if len(applied) > 1:
                            raise runtime.RuntimeErrorCode("ROLLBACK_FAILED", "fixture", unknown=True)
                    return original_kube(*args, document=document, **kwargs)
                runner.kube = kube
                failure = runtime.RuntimeErrorCode("ORIGINAL_FAILURE", "fixture", unknown=unknown)
                with patch.object(runtime, "configure_vault", side_effect=failure):
                    with self.assertRaises(runtime.RuntimeErrorCode) as caught:
                        runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
                self.assertEqual(caught.exception.code, "ORIGINAL_FAILURE")
                self.assertTrue(caught.exception.unknown)
                self.assertEqual(len(applied), 1 if unknown else 2)

    def test_initialization_has_explicit_inner_deadline_and_larger_outer_deadline(self):
        config = profile(); eso, storage = self.manifests(config); runner = FakeRunner()
        command = runner.command; observed = []
        def record(argv, **kwargs):
            if "init" in argv:
                observed.append((argv, kwargs))
            return command(argv, **kwargs)
        runner.command = record
        runtime.reconcile(config, "env-1", "aws", eso, storage, "configure", runner)
        self.assertEqual(len(observed), 1)
        argv, kwargs = observed[0]
        self.assertIn("VAULT_CLIENT_TIMEOUT=480s", argv)
        self.assertIn("VAULT_MAX_RETRIES=0", argv)
        hcl = next(doc for doc in runner.documents if doc["kind"] == "ConfigMap" and "vault.hcl" in doc.get("data", {}))["data"]["vault.hcl"]
        self.assertIn('max_request_duration = "420s"', hcl)
        self.assertEqual(kwargs["timeout"], 510)
        self.assertTrue(kwargs["sensitive"])

    def test_lost_or_malformed_sensitive_response_remains_unknown(self):
        for result in (runtime.subprocess.TimeoutExpired(["vault"], 510),
                       runtime.subprocess.CompletedProcess(["vault"], 0, "truncated", "")):
            with self.subTest(result=type(result).__name__):
                options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch.object(runtime.subprocess, "run", **options):
                    with self.assertRaises(runtime.RuntimeErrorCode) as caught:
                        runtime.Runner().command(["vault"], sensitive=True, json_output=True)
                self.assertTrue(caught.exception.unknown)

    def test_real_command_adapter_handles_delete_text_without_parsing_json(self):
        runner = runtime.Runner()
        with patch.object(runner, "command", return_value='pod "vault-0" deleted') as command:
            observed = runner.kube("-n", "railshot-secrets", "delete", "pod", "vault-0", "--wait=true", sensitive=True)
        self.assertIsNone(observed)
        self.assertNotIn("-o", command.call_args.args[0])
        self.assertTrue(command.call_args.kwargs["sensitive"])


if __name__ == "__main__": unittest.main()
