import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid


SPEC = importlib.util.spec_from_file_location(
    "openstack_route_worker", Path(__file__).resolve().parents[1] / "openstack_route_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class OctaviaCLI:
    """Only the worker's CLI subset, with observed OSC show-field formatting.

    Controller readbacks encode relations/tags as newline strings, project IDs
    as 32 hex, booleans as JSON booleans and absent pool references as null/"".
    Operating status deliberately stays OFFLINE: deployments follow routing.
    """

    def __init__(self, config):
        self.config = config
        self.objects = {kind: {} for kind in worker.KINDS}
        self.calls = []
        self.fail_create = None
        self.apply_failed_create = True
        self.fail_enable = False
        self.network_calls, self.network_rules = [], {}
        self.fail_network_create = False
        self.apply_network_create = True
        network = config["network"]
        self.group = {"id": network["runtime_security_group_id"], "project_id": network["runtime_project_id"]}
        self.ports = {}
        for role in ("runtime", "amphora"):
            self.ports[network[role + "_port_id"]] = {
                "id": network[role + "_port_id"], "device_id": network[role + "_server_id"],
                "project_id": network["runtime_project_id"] if role == "runtime" else config["project_id"],
                "device_owner": "compute:nova", "network_id": str(uuid.UUID(int=30)),
                "fixed_ips": [{"subnet_id": config["member_subnet_id"], "ip_address":
                    config["runtime_private_address"] if role == "runtime" else network["amphora_private_address"]}],
                "security_group_ids": [network["runtime_security_group_id"]] if role == "runtime" else []}
        self.lb = {"id": config["loadbalancer_id"], "project_id": config["project_id"],
                   "admin_state_up": True, "provisioning_status": "ACTIVE"}
        self.listener = {**self.lb, "id": config["listener_id"], "default_pool_id": None,
                         "protocol": "TERMINATED_HTTPS", "protocol_port": 443,
                         "loadbalancers": config["loadbalancer_id"],
                         "default_tls_container_ref": str(uuid.UUID(int=9))}

    def __call__(self, arguments, service=("loadbalancer",)):
        if not service:
            return self.network(arguments)
        assert service == ("loadbalancer",)
        self.calls.append(list(arguments))
        if arguments[0] == "show":
            return copy.deepcopy(self.lb)
        command, action, *tail = arguments
        if command == "listener":
            return copy.deepcopy(self.listener)
        kind = {"healthmonitor": "monitor", "l7policy": "policy", "l7rule": "rule"}.get(command, command)
        options, positionals = {}, []
        iterator = iter(tail)
        for token in iterator:
            if token in ("--wait", "--enable", "--disable", "--disable-backup"):
                options[token] = True
            elif token.startswith("-"):
                options[token] = next(iterator)
            else:
                positionals.append(token)
        objects = self.objects[kind]
        if action == "list":
            rows = [value for value in objects.values()
                    if ("--tags" not in options or options["--tags"] in value["tags"].splitlines()) and
                    (kind not in ("member", "rule") or value["parent"] == positionals[0])]
            return copy.deepcopy(rows)
        if action == "show":
            value = copy.deepcopy(objects[positionals[-1]])
            if kind in ("member", "rule"):
                assert value["parent"] == positionals[0]
            if kind == "pool":
                value["members"] = "\n".join(item["id"] for item in self.objects["member"].values()
                                              if item["parent"] == value["id"])
                value["healthmonitor_id"] = next((item["id"] for item in self.objects["monitor"].values()
                                                 if item["pools"] == value["id"]), "")
            if kind == "policy":
                value["rules"] = "\n".join(item["id"] for item in self.objects["rule"].values()
                                            if item["parent"] == value["id"])
            return value
        if action == "set":
            assert kind == "policy" and set(options) == {"--enable", "--wait"}
            if self.fail_enable:
                raise worker.RouteError("provider_response_unknown", "unknown")
            objects[positionals[0]]["admin_state_up"] = True
            return {}
        assert action == "create"
        value = {"id": str(uuid.uuid4()), "project_id": self.config["project_id"],
                 "provisioning_status": "ACTIVE", "operating_status": "OFFLINE",
                 "admin_state_up": "--enable" in options, "tags": options["--tag"]}
        for flag in ("name", "protocol", "address", "subnet-id", "http-method", "http-version",
                     "domain-name", "url-path", "expected-codes", "type", "compare-type", "value",
                     "action", "redirect-pool", "lb-algorithm"):
            if "--" + flag in options:
                key = "redirect_pool_id" if flag == "redirect-pool" else flag.replace("-", "_")
                value[key] = options["--" + flag]
        for flag in ("delay", "timeout", "max-retries", "protocol-port", "weight"):
            if "--" + flag in options:
                value[flag.replace("-", "_")] = int(options["--" + flag])
        if kind == "pool":
            assert "--listener" not in options
            assert options["--loadbalancer"] == self.config["loadbalancer_id"]
            value["loadbalancers"] = options["--loadbalancer"]
        if kind in ("member", "rule"):
            value["parent"] = positionals[0]
            value["backup" if kind == "member" else "invert"] = False
        if kind == "monitor":
            value["pools"] = positionals[0]
        if kind == "policy":
            assert options["--disable"]
            value["listener_id"] = positionals[0]
        fails = self.fail_create == kind
        if not fails or self.apply_failed_create:
            objects[value["id"]] = value
        if fails:
            self.fail_create = None
            raise worker.RouteError("provider_response_unknown", "unknown")
        return copy.deepcopy(value)

    def mutations(self):
        return [args for args in self.calls if len(args) > 1 and args[1] in ("create", "set")]

    def network(self, arguments):
        self.network_calls.append(list(arguments))
        if arguments[:2] == ["port", "show"]:
            return copy.deepcopy(self.ports[arguments[2]])
        if arguments[:3] == ["security", "group", "show"]:
            return copy.deepcopy(self.group)
        assert arguments[:3] == ["security", "group", "rule"]
        action = arguments[3]
        if action == "list":
            return [{"ID": key} for key in self.network_rules]
        if action == "show":
            return copy.deepcopy(self.network_rules[arguments[4]])
        assert action == "create"
        assert arguments[4] == self.group["id"]
        options = {}
        iterator = iter(arguments[5:])
        for token in iterator:
            options[token] = True if token == "--ingress" else next(iterator)
        rule = {"id": str(uuid.uuid4()), "security_group_id": arguments[4],
                "project_id": options["--project"], "direction": "ingress", "protocol": options["--protocol"],
                "ether_type": options["--ethertype"], "remote_ip_prefix": options["--remote-ip"],
                "port_range_min": int(options["--dst-port"]), "port_range_max": int(options["--dst-port"]),
                "remote_group_id": None, "remote_address_group_id": None, "description": options["--description"]}
        if not self.fail_network_create or self.apply_network_create:
            self.network_rules[rule["id"]] = rule
        if self.fail_network_create:
            self.fail_network_create = False
            raise worker.RouteError("provider_response_unknown", "unknown")
        return copy.deepcopy(rule)

    def network_mutations(self):
        return [args for args in self.network_calls if args[:4] == ["security", "group", "rule", "create"]]


class RouteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = {"version": 1, "project_id": "2a48957bf4b140fcb832de30baf156e8",
                       "loadbalancer_id": str(uuid.UUID(int=1)), "listener_id": str(uuid.UUID(int=2)),
                       "member_subnet_id": str(uuid.UUID(int=3)), "runtime_private_address": "10.0.0.17",
                       "base_domain": "railshot.io", "state_dir": str(Path(self.temporary.name).resolve())}
        self.config["network"] = {"runtime_project_id": "8de0a5b10e974e71b575cd16ff55d6df",
            "runtime_server_id": str(uuid.UUID(int=21)), "runtime_port_id": str(uuid.UUID(int=22)),
            "runtime_security_group_id": str(uuid.UUID(int=23)), "amphora_port_id": str(uuid.UUID(int=24)),
            "amphora_private_address": "10.0.0.40", "amphora_server_id": str(uuid.UUID(int=25))}
        self.request = {"application_id": "app-" + "a" * 24, "hostname": "first.railshot.io",
                        "private_address": "10.0.0.17", "node_port": 30123, "health_path": "/healthz"}
        self.cli = OctaviaCLI(self.config)
        for target, value in (("ROOT_UID", os.getuid()), ("verify_certificate", lambda *_: None)):
            mock = patch.object(worker, target, value)
            mock.start()
            self.addCleanup(mock.stop)

    def configure(self, request=None):
        return worker.configure(self.config, request or self.request, self.cli)

    def test_input_validation_before_provider_calls(self):
        for key, value in (("application_id", "../app"), ("hostname", "first.evil.test"),
                           ("hostname", "a.b.railshot.io"), ("private_address", "10.0.0.18"),
                           ("private_address", "127.0.0.1"), ("node_port", True), ("node_port", 29999),
                           ("health_path", "/../secret"), ("health_path", "/ok\ncommand")):
            with self.subTest(key=key, value=value), self.assertRaises(worker.RouteError):
                self.configure({**self.request, key: value})
        self.assertEqual(self.cli.calls, [])

    def test_two_apps_and_completed_replay_preserve_existing_resources(self):
        first = self.configure()
        old = copy.deepcopy(self.cli.objects)
        old_network = copy.deepcopy(self.cli.network_rules)
        second = self.configure({**self.request, "application_id": "app-" + "b" * 24,
                                 "hostname": "second.railshot.io", "node_port": 30124})
        for kind in worker.KINDS:
            self.assertEqual(self.cli.objects[kind][first["resources"][kind]], old[kind][first["resources"][kind]])
            self.assertNotEqual(first["resources"][kind], second["resources"][kind])
        count = len(self.cli.mutations())
        self.assertEqual(self.configure(), first)
        self.assertEqual(len(self.cli.mutations()), count)
        self.assertEqual(first["status"], "configured")
        self.assertIs(first["https_verified"], False)
        self.assertEqual(len(self.cli.objects["pool"]), 2)
        self.assertEqual(self.cli.network_rules[first["network_rule_id"]], old_network[first["network_rule_id"]])
        self.assertEqual(len(self.cli.network_mutations()), 2)

    def test_lost_create_response_recovers_by_reading_each_resource(self):
        for kind in worker.KINDS:
            with self.subTest(kind=kind):
                self.cli.fail_create = kind
                request = {**self.request, "application_id": "app-" + format(worker.KINDS.index(kind), "024x"),
                           "hostname": kind + ".railshot.io", "node_port": 30200 + worker.KINDS.index(kind)}
                self.assertEqual(self.configure(request)["status"], "configured")
        self.assertEqual(len([args for args in self.cli.mutations() if args[1] == "create"]), 25)

    def test_absent_uncertain_creation_is_never_repeated(self):
        self.cli.fail_create, self.cli.apply_failed_create = "pool", False
        for _ in range(2):
            with self.assertRaisesRegex(worker.RouteError, "creation_unresolved_pool") as error:
                self.configure()
            self.assertEqual(error.exception.status, "unknown")
        self.assertEqual(len(self.cli.mutations()), 1)
        state = json.loads((Path(self.config["state_dir"]) / (self.request["application_id"] + ".json")).read_text())
        self.assertEqual(state["steps"], {"pool": {"intent": True}})

    def test_lost_activation_is_not_repeated(self):
        self.cli.fail_enable = True
        for _ in range(2):
            with self.assertRaisesRegex(worker.RouteError, "activation_unresolved"):
                self.configure()
        self.assertEqual(len(self.cli.mutations()), 6)

    def test_preflight_and_certificate_block_before_creation(self):
        for changes in ({"default_pool_id": str(uuid.UUID(int=8))}, {"protocol": "HTTP"},
                        {"provisioning_status": "PENDING_UPDATE"}, {"project_id": "wrong"}):
            original = copy.deepcopy(self.cli.listener)
            self.cli.listener.update(changes)
            with self.assertRaises(worker.RouteError):
                self.configure()
            self.cli.listener = original
        with patch.object(worker, "verify_certificate", side_effect=worker.RouteError("hostname_certificate_not_ready")):
            with self.assertRaisesRegex(worker.RouteError, "hostname_certificate_not_ready"):
                self.configure()
        self.assertEqual(self.cli.mutations(), [])

    def test_conflicting_request_and_remote_drift_are_not_repaired(self):
        receipt = self.configure()
        count = len(self.cli.mutations())
        with self.assertRaisesRegex(worker.RouteError, "application_intent_conflict"):
            self.configure({**self.request, "node_port": 30124})
        self.cli.objects["member"][receipt["resources"]["member"]]["address"] = "10.0.0.18"
        with self.assertRaisesRegex(worker.RouteError, "resource_mismatch_address"):
            self.configure()
        self.assertEqual(len(self.cli.mutations()), count)

    def test_relation_formats_are_strict(self):
        identifier = str(uuid.UUID(int=1))
        self.assertEqual(worker.relation_ids(identifier), {identifier})
        self.assertEqual(worker.relation_ids([{"id": identifier}]), {identifier})
        self.assertEqual(worker.relation_ids(""), set())
        with self.assertRaises(worker.RouteError):
            worker.relation_ids("not-an-id")

    def test_network_identity_mismatch_blocks_before_any_mutation(self):
        network = self.config["network"]
        for role, changes in (("runtime", {"device_id": str(uuid.UUID(int=99))}),
                              ("runtime", {"security_group_ids": []}),
                              ("amphora", {"project_id": network["runtime_project_id"]}),
                              ("amphora", {"fixed_ips": []})):
            port = self.cli.ports[network[role + "_port_id"]]
            original = copy.deepcopy(port)
            port.update(changes)
            with self.assertRaises(worker.RouteError):
                self.configure()
            self.cli.ports[network[role + "_port_id"]] = original
        self.assertEqual(self.cli.mutations() + self.cli.network_mutations(), [])

    def test_exact_existing_network_rule_reused_and_broad_rule_preserved(self):
        state_path = Path(self.config["state_dir"]) / "network-test.json"
        first = worker.ensure_network_rule(self.config, self.request, {}, state_path, self.cli)
        broad = copy.deepcopy(self.cli.network_rules[first])
        broad.update(id=str(uuid.uuid4()), remote_ip_prefix="0.0.0.0/0", port_range_min=30000,
                     port_range_max=32767, description="existing operator rule")
        self.cli.network_rules[broad["id"]] = broad
        self.assertEqual(worker.ensure_network_rule(self.config, self.request, {}, state_path, self.cli), first)
        self.assertEqual(len(self.cli.network_mutations()), 1)
        self.assertEqual(self.cli.network_rules[broad["id"]], broad)
        narrow = self.cli.network_rules[first]
        self.assertEqual((narrow["remote_ip_prefix"], narrow["port_range_min"], narrow["port_range_max"]),
                         ("10.0.0.40/32", 30123, 30123))
        self.assertEqual(narrow["description"], "railshot:" + self.request["application_id"])

    def test_network_lost_create_response_recovers_without_duplicate(self):
        self.cli.fail_network_create = True
        first = self.configure()
        self.assertEqual(self.configure(), first)
        self.assertEqual(len(self.cli.network_mutations()), 1)

    def test_unresolved_network_create_blocks_lb_and_is_not_repeated(self):
        self.cli.fail_network_create, self.cli.apply_network_create = True, False
        for _ in range(2):
            with self.assertRaisesRegex(worker.RouteError, "network_creation_unresolved"):
                self.configure()
        self.assertEqual(len(self.cli.network_mutations()), 1)
        self.assertEqual(self.cli.mutations(), [])

    def test_completed_network_rule_drift_is_not_repaired(self):
        receipt = self.configure()
        rule = self.cli.network_rules[receipt["network_rule_id"]]
        rule["port_range_max"] += 1
        with self.assertRaisesRegex(worker.RouteError, "network_rule_readback_mismatch"):
            self.configure()
        self.assertEqual(len(self.cli.network_mutations()), 1)


class CertificateTests(unittest.TestCase):
    def test_fixed_public_certificate_and_ca_only(self):
        reference = str(uuid.UUID(int=9))
        def info(path):
            return SimpleNamespace(st_mode=(stat.S_IFDIR | 0o700 if path.name in ("certificates", "origin-ca") else
                                            stat.S_IFREG | 0o600),
                                   st_uid=123 if path.suffix == ".crt" or path.name == "certificates" else 0)
        with patch.object(Path, "lstat", info), patch.object(Path, "resolve", lambda path: path), \
                patch.object(worker.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=123)), \
                patch.object(worker.subprocess, "run") as run:
            worker.verify_certificate(reference, "first.railshot.io")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/openssl", "verify", "-CAfile",
            "/opt/railshot/octavia/origin-ca/ca.pem", "-verify_hostname", "first.railshot.io", "-purpose",
            "sslserver", "/var/lib/octavia/certificates/" + reference + ".crt"])

    def test_cli_failure_never_includes_raw_output(self):
        error = subprocess.CalledProcessError(1, "unused", output="private", stderr="private")
        with patch.object(worker.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(worker.RouteError, "^provider_response_unknown$"):
                worker.run_cli(["show", str(uuid.UUID(int=1)), "-f", "json"])


if __name__ == "__main__":
    unittest.main()
