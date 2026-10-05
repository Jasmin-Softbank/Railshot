import base64
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
SPEC = importlib.util.spec_from_file_location("secrets_delivery", ROOT / "deployment/scripts/secrets_delivery.py")
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


class FakeExecutor:
    instances = []

    def __init__(self, target, app):
        self.target, self.app, self.objects, self.vault_writes = target, app, {}, []
        self.secret_values, self.external_reads_before_ready = {}, 0
        self.__class__.instances.append(self)

    def vault_write(self, path, values):
        self.vault_writes.append((path, copy.deepcopy(values)))
        self.secret_values.update(values)

    def vault(self, args, payload=None, **kwargs):
        self.vault_calls = getattr(self, "vault_calls", []) + [(copy.deepcopy(args), payload)]
        return ""

    def kube(self, *args, document=None):
        namespace = self.app["namespace"]
        if "apply" in args:
            key = (document["kind"].lower(), document["metadata"]["name"])
            value = copy.deepcopy(document)
            if document["kind"] == "ExternalSecret":
                value["metadata"]["uid"] = "external-secret-uid"
                value["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
                secret = {"metadata": {"labels": copy.deepcopy(document["spec"]["target"]["template"]["metadata"]["labels"]),
                          "ownerReferences": [{"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
                          "name": document["metadata"]["name"], "uid": value["metadata"]["uid"], "controller": True}]},
                          "immutable": True, "data": {row["secretKey"]: base64.b64encode(
                              self.secret_values[row["secretKey"]].encode()).decode() for row in document["spec"]["data"]}}
                self.objects[("secret", document["spec"]["target"]["name"])] = secret
            if document["kind"] == "SecretStore":
                value["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
            self.objects[key] = value
            return value
        if "patch" in args:
            name = args[args.index("deployment") + 1]
            deployment = self.objects.setdefault(("deployment", name), self.deployment())
            patch = json.loads(args[args.index("-p") + 1])
            template = deployment["spec"]["template"]
            template["metadata"]["annotations"] = patch[1]["value"]
            template["spec"]["containers"][0]["env"] = patch[2]["value"]
            template["spec"]["containers"][0]["envFrom"] = patch[3]["value"]
            return deployment
        kind, name = args[args.index("get") + 1:args.index("get") + 3]
        if kind == "namespace":
            return {"metadata": {"name": name}}
        if kind == "deployment":
            return self.objects.setdefault((kind, name), self.deployment())
        if kind == "endpoints":
            return {"subsets": [{"addresses": [{"ip": "10.0.0.2"}]}]}
        if kind == "service":
            return {"spec": {"ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}}
        if kind == "externalsecret" and self.external_reads_before_ready and (kind, name) in self.objects:
            self.external_reads_before_ready -= 1
            value = copy.deepcopy(self.objects[(kind, name)])
            value["status"] = {"conditions": [{"type": "Ready", "status": "False"}]}
            return value
        if "--ignore-not-found" in args:
            return self.objects.get((kind, name))
        return self.objects[(kind, name)]

    def kube_raw(self, path):
        self.last_raw = path; return True

    def deployment(self):
        return {"metadata": {"generation": 3, "resourceVersion": "9"}, "spec": {"replicas": 1,
            "template": {"metadata": {"annotations": {}}, "spec": {"containers": [{"name": self.app["container"],
            "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}}}]}}},
            "status": {"observedGeneration": 3, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1,
                       "availableReplicas": 1, "unavailableReplicas": 0}}


class DeliveryTests(unittest.TestCase):
    def test_expired_approle_rotation_uses_scoped_issuer_and_retains_old_until_new_login(self):
        root = Path(tempfile.mkdtemp()); self.addCleanup(lambda: __import__('shutil').rmtree(root))
        path = root / 'delivery.json'; old = {'role_id': 'role', 'secret_id': 'expired'}
        delivery.atomic_private(path, old)
        target = {'environment_id': 'env-1', 'vault': {'token_file': str(path), 'custody_config_file': '/unused'}}
        events = []; material = {}
        class Client:
            def lookup(self, *args): return None
            def store(self, payload): material.update(payload['material']); events.append('stored'); return {'stored': True, 'receipt_id': 'receipt'}
            def recover(self, *args): return {'stored': True, 'receipt_id': 'receipt', 'material': material}
        class Executor:
            def issuer_login(self): events.append('issuer'); return 'issuer-token'
            def _vault_exec(self, token, args, **kwargs):
                self_test.assertEqual(token, 'issuer-token')
                if args[-1].endswith('role-id'): return {'data': {'role_id': 'role'}}
                if 'auth/approle/role/railshot-delivery/secret-id/destroy' in args:
                    self_test.assertEqual(json.loads(path.read_text())['secret_id'], 'fresh'); events.append('retired'); return ''
                return {'data': {'secret_id': 'fresh'}}
            def login(self, credential):
                self_test.assertEqual(json.loads(path.read_text()), old)
                self_test.assertEqual(credential['secret_id'], 'fresh'); events.append('new-login'); return 'new-token'
        self_test = self
        result = delivery.rotate_delivery_credential(target, Executor(), 'rotation-2', retire_previous=True, client=Client())
        self.assertTrue(result['previous_retired']); self.assertEqual(events, ['issuer', 'stored', 'new-login', 'retired'])
        self.assertEqual(json.loads(path.read_text())['secret_id'], 'fresh')

    def test_handoff_failure_preserves_existing_management_credential(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'delivery.json'; old = {'role_id': 'old', 'secret_id': 'still-valid'}
            delivery.atomic_private(path, old)
            target = {'environment_id': 'env-1', 'vault': {'token_file': str(path)}}
            class Client:
                def recover(self, *args): return {'stored': True, 'receipt_id': 'receipt', 'material': {'role_id': 'new', 'secret_id': 'rejected'}}
            with self.assertRaises(delivery.DeliveryError):
                delivery.custody_handoff(target, lambda _: None, client=Client())
            self.assertEqual(json.loads(path.read_text()), old)

    def setUp(self):
        FakeExecutor.instances.clear()
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        ca = Path(self.temp.name) / "vault-ca.crt"; ca.write_text("-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"); ca.chmod(0o600)
        self.config = {"version": 1, "environments": {"env-1": {
            "registry_file": "/private/targets.json",
            "vault": {"namespace": "railshot-secrets", "pod": "vault-0", "mount": "railshot", "auth_mount": "kubernetes",
                      "token_file": "/private/token", "ca_file": str(ca)},
            "applications": {"app-123": {"app": "demo", "namespace": "app-demo", "deployment": "demo",
                "service": "demo", "container": "web", "service_account": "default",
                "secret_store": {"kind": "SecretStore", "name": "railshot-vault"}}}}}}
        self.request = {"version": 1, "operation_id": "op-1", "project_id": "project-1", "binding_id": "binding-1",
            "revision_id": "revision-7", "environment_id": "env-1", "application_id": "app-123", "app": "demo",
            "phase": "prepare", "variables": [
                {"name": "LOG_LEVEL", "kind": "plain", "value": "info", "required": True},
                {"name": "API_KEY", "kind": "secret", "value": "do-not-leak", "required": True}]}

    def test_prepare_writes_versioned_values_and_returns_metadata_only(self):
        result = delivery.deliver(self.config, self.request, FakeExecutor)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["observed_revision_id"], "revision-7")
        self.assertEqual(result["configuration"]["plain_names"], ["LOG_LEVEL"])
        self.assertEqual(result["configuration"]["secret_names"], ["API_KEY"])
        self.assertNotIn("do-not-leak", json.dumps(result))
        executor = FakeExecutor.instances[-1]
        self.assertEqual(executor.vault_writes, [("projects/project-1/revisions/revision-7", {"API_KEY": "do-not-leak"})])
        configmap = executor.objects[("configmap", result["configuration"]["configmap_name"])]
        self.assertTrue(configmap["immutable"])
        self.assertEqual(configmap["data"], {"LOG_LEVEL": "info"})
        external = executor.objects[("externalsecret", result["configuration"]["external_secret_name"])]
        self.assertTrue(external["spec"]["target"]["immutable"])
        self.assertNotIn("immutable", external["spec"]["target"]["template"])

    def test_prepare_waits_for_external_secret_and_rejects_wrong_secret_value(self):
        class DelayedExecutor(FakeExecutor):
            def __init__(self, target, app):
                super().__init__(target, app); self.external_reads_before_ready = 1
        with patch.object(delivery.time, "sleep", return_value=None) as sleep:
            prepared = delivery.deliver(self.config, self.request, DelayedExecutor)
        sleep.assert_called_once_with(2)
        executor = DelayedExecutor.instances[-1]
        secret = executor.objects[("secret", prepared["configuration"]["secret_name"])]
        secret["data"]["API_KEY"] = base64.b64encode(b"wrong-value").decode()
        self.request["phase"] = "verify"
        class Reuse:
            def __new__(cls, target, app): return executor
        with self.assertRaisesRegex(delivery.DeliveryError, "Secret revision differs"):
            delivery.deliver(self.config, self.request, Reuse)

    def test_external_secret_server_defaults_do_not_break_owned_readback(self):
        class DefaultingExecutor(FakeExecutor):
            def kube(self, *args, document=None):
                value = super().kube(*args, document=document)
                if document is None and value is not None and "get" in args and "externalsecret" in args:
                    value = copy.deepcopy(value)
                    value["spec"]["target"].update(deletionPolicy="Retain")
                    value["spec"]["target"]["template"].update(engineVersion="v2", mergePolicy="Replace")
                    for row in value["spec"]["data"]:
                        row["remoteRef"].update(conversionStrategy="Default", decodingStrategy="None", metadataPolicy="None")
                return value
        result = delivery.deliver(self.config, self.request, DefaultingExecutor)
        self.assertEqual(result["status"], "succeeded")

    def test_apply_pins_exact_refs_then_readback_checks_workload_and_service(self):
        prepared = delivery.deliver(self.config, self.request, FakeExecutor)
        executor = FakeExecutor.instances[-1]
        self.request["phase"] = "apply"
        # Reuse the prepared target state like a real second process observing the cluster.
        class Reuse:
            def __new__(cls, target, app): return executor
        result = delivery.deliver(self.config, self.request, Reuse)
        self.assertTrue(result["checks"]["workload_ready"])
        deployment = executor.objects[("deployment", "demo")]
        self.assertEqual(deployment["spec"]["template"]["metadata"]["annotations"]["railshot.io/configuration-revision"], "revision-7")
        self.assertEqual(deployment["spec"]["template"]["spec"]["containers"][0]["envFrom"], [
            {"configMapRef": {"name": prepared["configuration"]["configmap_name"]}},
            {"secretRef": {"name": prepared["configuration"]["secret_name"]}}])

    def test_verify_requires_declared_refs_and_never_falls_back_to_latest(self):
        delivery.deliver(self.config, self.request, FakeExecutor)
        executor = FakeExecutor.instances[-1]
        self.request["phase"] = "verify"
        class Reuse:
            def __new__(cls, target, app): return executor
        with self.assertRaisesRegex(delivery.DeliveryError, "workload does not pin"):
            delivery.deliver(self.config, self.request, Reuse)

    def test_reserved_database_and_platform_variables_are_rejected_before_side_effects(self):
        for name in sorted(delivery.RESERVED):
            changed = copy.deepcopy(self.request)
            changed["variables"] = [{"name": name, "kind": "plain", "value": "x", "required": True}]
            with self.subTest(name=name), self.assertRaises(delivery.DeliveryError):
                delivery.deliver(self.config, changed, FakeExecutor)

    def test_unregistered_target_and_arbitrary_request_paths_are_rejected(self):
        changed = copy.deepcopy(self.request); changed["environment_id"] = "env-other"
        with self.assertRaisesRegex(delivery.DeliveryError, "not registered"):
            delivery.deliver(self.config, changed, FakeExecutor)
        changed = copy.deepcopy(self.request); changed["vault_address"] = "https://attacker.invalid"
        with self.assertRaisesRegex(delivery.DeliveryError, "fields differ"):
            delivery.deliver(self.config, changed, FakeExecutor)

    def test_private_file_guard_rejects_group_readable_secret_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"; path.write_text(json.dumps(self.request)); path.chmod(0o640)
            with self.assertRaisesRegex(delivery.DeliveryError, "0600"):
                delivery.private_json(path)

    def test_dynamic_environment_application_uses_digest_bound_registration(self):
        root = Path(self.temp.name); home = root / "environment-operation-1"; home.mkdir(mode=0o700)
        registry = home / "targets.json"; registry.write_text("{}\n"); registry.chmod(0o600)
        app, environment_id = "demo", "env-1"
        cd = {"version": 1, "targets": {environment_id: {"app": app,
              "target": {"id": environment_id, "namespace": "dedicated-demo"}}}}
        cd_raw = json.dumps(cd, separators=(",", ":")).encode()
        (home / "cd.json").write_bytes(cd_raw); (home / "cd.json").chmod(0o600)
        registration = {"status": "succeeded", "target_id": environment_id,
                        "environment_id": "environment-operation-1",
                        "cd_sha256": __import__("hashlib").sha256(cd_raw).hexdigest()}
        registration_raw = json.dumps(registration, separators=(",", ":")).encode()
        registration_path = home / "registration.json"
        registration_path.write_bytes(registration_raw); registration_path.chmod(0o600)
        ledger = {"version": 1, "status": "succeeded", "environment_id": environment_id,
                  "environment_operation_id": "environment-operation-1",
                  "registration_file": str(registration_path),
                  "registration_sha256": __import__("hashlib").sha256(registration_raw).hexdigest()}
        ledger_path = home / "secrets-registration.json"
        ledger_path.write_text(json.dumps(ledger)); ledger_path.chmod(0o600)
        application_id = "envapp-" + __import__("hashlib").sha256(
            json.dumps([environment_id, app], separators=(",", ":")).encode()).hexdigest()[:24]
        target = copy.deepcopy(self.config["environments"][environment_id])
        target.pop("applications"); target.update(environment_id=environment_id,
                                                   environment_registry_dir=str(root), registry_file=str(registry))
        request = {**self.request, "application_id": application_id}
        selected, resolved = delivery.select_target({"version": 1, **target}, request)
        self.assertEqual(selected["environment_id"], environment_id)
        self.assertEqual(resolved["namespace"], "dedicated-demo")
        (home / "cd.json").write_text(json.dumps({"version": 1, "targets": {}}))
        with self.assertRaisesRegex(delivery.DeliveryError, "CD registration differs"):
            delivery.select_target({"version": 1, **target}, request)


if __name__ == "__main__":
    unittest.main()
