#!/usr/bin/env python3
"""Controller-only Octavia routes. Never provisions a listener, TLS, or DNS.

The root-owned config fixes one project/LB/listener/runtime/subnet/domain, the
runtime and amphora network identities, and a private state_dir. Keep that
directory across deployments: an intent without an
ID permits discovery only, never another create. JSON stdout contains no CLI
output. The existing cloud-cli.py owns authentication. An application's full
request is immutable, including health_path; changing it needs operator action.
"""

import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import uuid


CONFIG = Path("/opt/railshot/octavia/route-config.json")
CLI = "/opt/railshot/octavia/cloud-cli.py"
ROOT_UID = 0
KINDS = ("pool", "member", "monitor", "policy", "rule")


class RouteError(ValueError):
    def __init__(self, reason, status="blocked"):
        super().__init__(reason)
        self.status = status


def require(condition, reason):
    if not condition:
        raise RouteError(reason)


def is_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def is_project(value):
    return isinstance(value, str) and (is_uuid(value) or re.fullmatch(r"[0-9a-f]{32}", value))


def is_private_ipv4(value):
    try:
        ip = ipaddress.IPv4Address(value) if isinstance(value, str) else None
    except ipaddress.AddressValueError:
        return False
    return ip is not None and any(ip in ipaddress.ip_network(cidr) for cidr in
                                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def checked_request(config, request):
    """Validate the public adapter/worker contract without accessing credentials."""
    keys = {"application_id", "hostname", "private_address", "node_port", "health_path"}
    require(isinstance(request, dict) and set(request) == keys, "invalid_request_keys")
    app, host = request["application_id"], request["hostname"]
    require(isinstance(app, str) and re.fullmatch(r"app-[0-9a-f]{24}", app), "invalid_application_id")
    domain = config.get("base_domain", "")
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    require(isinstance(domain, str) and len(domain) <= 190 and
            re.fullmatch(rf"{label}(?:\.{label})+", domain), "invalid_base_domain")
    require(isinstance(host, str) and re.fullmatch(rf"{label}\.{re.escape(domain)}", host),
            "invalid_hostname")
    address = request["private_address"]
    require(is_private_ipv4(address) and
            address == config.get("runtime_private_address"), "invalid_private_address")
    port = request["node_port"]
    require(type(port) is int and 30000 <= port <= 32767, "invalid_node_port")
    path = request["health_path"]
    require(isinstance(path, str) and len(path) <= 1024 and
            re.fullmatch(r"/[A-Za-z0-9/_~.-]*", path) and "//" not in path and
            not {".", ".."}.intersection(path.split("/")), "invalid_health_path")
    return dict(request)


def checked_config(config):
    keys = {"version", "project_id", "loadbalancer_id", "listener_id", "member_subnet_id",
            "runtime_private_address", "base_domain", "state_dir", "network"}
    require(isinstance(config, dict) and set(config) == keys and
            type(config["version"]) is int and config["version"] == 1, "invalid_config")
    require(is_project(config["project_id"]), "invalid_config_identity")
    for key in ("loadbalancer_id", "listener_id", "member_subnet_id"):
        require(is_uuid(config[key]), "invalid_config_identity")
    network = config["network"]
    identities = {"runtime_server_id", "runtime_port_id", "runtime_security_group_id",
                  "amphora_port_id", "amphora_server_id"}
    require(isinstance(network, dict) and set(network) == identities | {
        "runtime_project_id", "amphora_private_address"}, "invalid_network_configuration")
    require(all(is_uuid(network[key]) for key in identities) and
            is_project(network["runtime_project_id"]) and is_private_ipv4(network["amphora_private_address"])
            and network["amphora_private_address"] != config["runtime_private_address"],
            "invalid_network_configuration")
    require(isinstance(config["state_dir"], str), "invalid_state_directory")
    path = Path(config["state_dir"])
    require(path.is_absolute() and path == path.resolve(), "invalid_state_directory")
    return config


def private_stat(info):
    require(info.st_uid == ROOT_UID and not info.st_mode & 0o077, "insecure_private_file")


def read_private(path):
    require(path.is_absolute() and path == path.resolve(), "unsafe_private_path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        private_stat(info)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 1048576,
                "invalid_private_file")
        return json.load(stream)


def save(path, record):
    fd, temporary = tempfile.mkstemp(prefix=".route-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_cli(arguments, service=("loadbalancer",)):
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-I", CLI, *service, *arguments],
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 "HOME": "/root", "LANG": "C.UTF-8"},
            capture_output=True, text=True, timeout=180, check=True)
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except (subprocess.SubprocessError, OSError, ValueError):
        # Provider output may contain authentication details. Never return it.
        raise RouteError("provider_response_unknown", "unknown") from None


def relation_ids(value):
    if isinstance(value, str):
        ids = value.splitlines()
        require(all(is_uuid(item) for item in ids), "invalid_provider_relation")
        return set(ids)
    require(isinstance(value, list) and all(isinstance(row, dict) and
            is_uuid(row.get("id")) for row in value), "invalid_provider_relation")
    return {row["id"] for row in value}


def verify_certificate(reference, hostname):
    require(is_uuid(reference), "local_listener_certificate_required")
    certificate = Path("/var/lib/octavia/certificates") / (reference + ".crt")
    ca = Path("/opt/railshot/octavia/origin-ca/ca.pem")
    try:
        octavia_uid = pwd.getpwnam("octavia").pw_uid
    except KeyError:
        octavia_uid = None
    try:
        for path in (certificate, ca):
            owners = {ROOT_UID, octavia_uid} if path == certificate else {ROOT_UID}
            parent = path.parent.lstat()
            require(stat.S_ISDIR(parent.st_mode) and parent.st_uid in owners and
                    not parent.st_mode & 0o022, "unsafe_certificate_directory")
            info = path.lstat()
            require(path.resolve() == path and stat.S_ISREG(info.st_mode) and
                    info.st_uid in owners and not info.st_mode & 0o022,
                    "unsafe_certificate_file")
        subprocess.run(["/usr/bin/openssl", "verify", "-CAfile", str(ca),
                        "-verify_hostname", hostname, "-purpose", "sslserver", str(certificate)],
                       capture_output=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        raise RouteError("hostname_certificate_not_ready") from None


def ensure_network_rule(config, request, record, path, call):
    """One fixed amphora /32 -> one runtime TCP NodePort; no SG/port changes."""
    network = config["network"]
    group = network["runtime_security_group_id"]

    def native(arguments):
        return call([*arguments, "-f", "json"], service=())

    ports = []
    for role, project, address in (
            ("runtime", network["runtime_project_id"], request["private_address"]),
            ("amphora", config["project_id"], network["amphora_private_address"])):
        port = native(["port", "show", network[role + "_port_id"]])
        require(isinstance(port, dict) and port.get("id") == network[role + "_port_id"] and
                port.get("project_id") == project and port.get("device_id") == network[role + "_server_id"]
                and port.get("device_owner") == "compute:nova", "network_port_identity_mismatch")
        require(isinstance(port.get("fixed_ips"), list) and any(
            isinstance(ip, dict) and ip.get("ip_address") == address and
            ip.get("subnet_id") == config["member_subnet_id"] for ip in port["fixed_ips"]),
            "network_port_address_mismatch")
        ports.append(port)
    require(is_uuid(ports[0].get("network_id")) and ports[0]["network_id"] == ports[1].get("network_id")
            and isinstance(ports[0].get("security_group_ids"), list) and
            group in ports[0]["security_group_ids"], "runtime_security_group_attachment_mismatch")
    security_group = native(["security", "group", "show", group])
    require(isinstance(security_group, dict) and security_group.get("id") == group and
            security_group.get("project_id") == network["runtime_project_id"], "security_group_identity_mismatch")
    expected = {"security_group_id": group, "project_id": network["runtime_project_id"],
                "direction": "ingress", "protocol": "tcp", "port_range_min": request["node_port"],
                "port_range_max": request["node_port"],
                "remote_ip_prefix": network["amphora_private_address"] + "/32"}
    description = "railshot:" + request["application_id"]

    def exact(rule):
        return (isinstance(rule, dict) and all(rule.get(key) == value for key, value in expected.items())
                and rule.get("ether_type", rule.get("ethertype")) == "IPv4" and
                rule.get("remote_group_id") in (None, "") and rule.get("remote_address_group_id") in (None, ""))

    def find():
        rows = native(["security", "group", "rule", "list", group, "--protocol", "tcp",
                       "--ethertype", "IPv4", "--ingress", "-c", "ID"])
        require(isinstance(rows, list), "invalid_network_rule_list")
        found = []
        for row in rows:
            require(isinstance(row, dict) and is_uuid(row.get("ID")), "invalid_network_rule_id")
            rule = native(["security", "group", "rule", "show", row["ID"]])
            if exact(rule):
                require(rule.get("description") == description, "network_rule_owner_mismatch")
                found.append(row["ID"])
        require(len(found) <= 1, "ambiguous_network_rule")
        return found[0] if found else None

    step = record.get("network")
    rule_id = step.get("id") if step else None
    if not rule_id:
        rule_id = find()
        if not rule_id:
            if step is not None:
                raise RouteError("network_creation_unresolved", "unknown")
            record["network"] = {"intent": True}
            save(path, record)
            try:
                response = native(["security", "group", "rule", "create", group, "--project", network["runtime_project_id"],
                                   "--ingress", "--ethertype", "IPv4", "--protocol", "tcp", "--remote-ip",
                                   expected["remote_ip_prefix"], "--dst-port", str(request["node_port"]),
                                   "--description", description])
                if isinstance(response, dict) and is_uuid(response.get("id")):
                    rule_id = response["id"]
                    record["network"]["id"] = rule_id
                    save(path, record)
            except RouteError:
                pass
            rule_id = rule_id or find()
            if not rule_id:
                raise RouteError("network_creation_unresolved", "unknown")
    require(is_uuid(rule_id), "invalid_network_rule_id")
    rule = native(["security", "group", "rule", "show", rule_id])
    require(rule.get("id") == rule_id and exact(rule) and rule.get("description") == description,
            "network_rule_readback_mismatch")
    record["network"] = {"intent": True, "id": rule_id}
    save(path, record)
    return rule_id


def configure(config, request, call=run_cli):
    config = checked_config(config)
    request = checked_request(config, request)
    directory = Path(config["state_dir"])
    directory.mkdir(mode=0o700, exist_ok=True)
    private_stat(directory.stat())
    lock = os.open(directory / (config["loadbalancer_id"] + ".lock"),
                   os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_stat(os.fstat(lock))
        require(stat.S_ISREG(os.fstat(lock).st_mode), "invalid_lock_file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        return configure_locked(config, request, directory, call)
    finally:
        os.close(lock)


def configure_locked(config, request, directory, call):
    app, lb, listener = request["application_id"], config["loadbalancer_id"], config["listener_id"]
    fingerprint = hashlib.sha256(json.dumps({"config": config, "request": request},
                                           sort_keys=True).encode()).hexdigest()
    tag = "railshot-" + fingerprint
    path = directory / (app + ".json")
    if path.exists():
        record = read_private(path)
        require(record.get("fingerprint") == fingerprint and record.get("request") == request,
                "application_intent_conflict")
    else:
        record = {"fingerprint": fingerprint, "request": request, "steps": {}}
        for other in directory.glob("app-*.json"):
            previous = read_private(other)["request"]
            require(previous["hostname"] != request["hostname"] and
                    previous["node_port"] != request["node_port"], "application_route_conflict")
        save(path, record)

    def show(kind, resource_id):
        command = {"monitor": "healthmonitor", "policy": "l7policy", "rule": "l7rule"}.get(kind, kind)
        parent = ([record["steps"]["pool"]["id"]] if kind == "member" else
                  [record["steps"]["policy"]["id"]] if kind == "rule" else [])
        return call([command, "show", *parent, resource_id, "-f", "json"])

    def matches(actual, expected):
        require(isinstance(actual, dict), "invalid_provider_response")
        for key, value in expected.items():
            found = actual.get(key)
            equal = (type(found) is bool and found == value) if type(value) is bool else str(found) == str(value)
            require(key in actual and equal, "resource_mismatch_" + key)

    tls_reference = None

    def preflight():
        matches(call(["show", lb, "-f", "json"]), {
            "id": lb, "project_id": config["project_id"],
            "admin_state_up": True, "provisioning_status": "ACTIVE"})
        existing = call(["listener", "show", listener, "-f", "json"])
        matches(existing, {"id": listener, "project_id": config["project_id"],
                           "protocol": "TERMINATED_HTTPS", "protocol_port": 443,
                           "admin_state_up": True, "provisioning_status": "ACTIVE"})
        require(relation_ids(existing.get("loadbalancers")) == {lb}, "listener_lb_mismatch")
        require("default_pool_id" in existing and existing["default_pool_id"] in (None, ""),
                "listener_has_default_pool")
        if tls_reference is not None:
            require(existing.get("default_tls_container_ref") == tls_reference,
                    "listener_certificate_changed")
        return existing

    def discover(kind):
        command = {"monitor": "healthmonitor", "policy": "l7policy", "rule": "l7rule"}.get(kind, kind)
        scope = {"pool": ["--loadbalancer", lb], "member": [record["steps"].get("pool", {}).get("id", "")],
                 "monitor": [], "policy": ["--listener", listener],
                 "rule": [record["steps"].get("policy", {}).get("id", "")]}
        rows = call([command, "list", *scope[kind], "--tags", tag, "-f", "json"])
        require(isinstance(rows, list) and len(rows) <= 1 and all(
            isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows),
            "ambiguous_resource_discovery")
        return rows[0]["id"] if rows else None

    def verified(kind, resource_id):
        actual = show(kind, resource_id)
        expected = {"id": resource_id, "project_id": config["project_id"],
                    "provisioning_status": "ACTIVE"}
        if kind != "policy":
            expected["admin_state_up"] = True
        if kind != "rule":
            expected["name"] = app
        if kind == "pool":
            expected.update(protocol="HTTP", lb_algorithm="ROUND_ROBIN")
            require(relation_ids(actual.get("loadbalancers")) == {lb}, "pool_lb_mismatch")
        elif kind == "member":
            expected.update(address=request["private_address"], protocol_port=request["node_port"],
                            subnet_id=config["member_subnet_id"], weight=1, backup=False)
        elif kind == "monitor":
            expected.update(type="HTTP", delay=10, timeout=5, max_retries=3, http_method="GET",
                            http_version="1.1", domain_name=request["hostname"],
                            url_path=request["health_path"], expected_codes="200")
            require(relation_ids(actual.get("pools")) == {record["steps"]["pool"]["id"]},
                    "monitor_pool_mismatch")
        elif kind == "policy":
            expected.update(listener_id=listener, action="REDIRECT_TO_POOL",
                            redirect_pool_id=record["steps"]["pool"]["id"])
            require(type(actual.get("admin_state_up")) is bool, "invalid_policy_state")
            require(not actual["admin_state_up"] or record.get("activation_intent"),
                    "unexpected_enabled_policy")
        elif kind == "rule":
            expected.update(type="HOST_NAME", compare_type="EQUAL_TO", invert=False,
                            value=request["hostname"])
        matches(actual, expected)
        tags = actual.get("tags")
        tags = tags.splitlines() if isinstance(tags, str) else tags
        require(isinstance(tags, list) and tag in tags, "resource_owner_mismatch")
        return actual

    tls_reference = preflight().get("default_tls_container_ref")
    verify_certificate(tls_reference, request["hostname"])
    # Protect operator-created routes as well as applications in our journal.
    for existing in call(["l7policy", "list", "--listener", listener, "-f", "json"]):
        if existing["id"] == record["steps"].get("policy", {}).get("id") or existing.get("admin_state_up") is False:
            continue
        rules = call(["l7rule", "list", existing["id"], "-f", "json"])
        hosts = [rule for rule in rules if rule.get("type") == "HOST_NAME" and
                 rule.get("compare_type") == "EQUAL_TO" and rule.get("invert") is False]
        require(hosts and all(rule.get("value") != request["hostname"] for rule in hosts),
                "existing_listener_route_conflict")
    network_rule_id = ensure_network_rule(config, request, record, path, call)
    for kind in KINDS:
        step = record["steps"].get(kind)
        if step is not None:
            resource_id = step.get("id") or discover(kind)
            if not resource_id:
                raise RouteError("creation_unresolved_" + kind, "unknown")
        else:
            require(not discover(kind), "unrecorded_resource_" + kind)
            preflight()
            pool = record["steps"].get("pool", {}).get("id", "")
            policy = record["steps"].get("policy", {}).get("id", "")
            commands = {
                "pool": ["pool", "create", "--name", app, "--protocol", "HTTP",
                         "--loadbalancer", lb, "--lb-algorithm", "ROUND_ROBIN", "--enable"],
                "member": ["member", "create", "--name", app, "--address", request["private_address"],
                           "--subnet-id", config["member_subnet_id"], "--protocol-port", str(request["node_port"]),
                           "--weight", "1", "--disable-backup", "--enable", pool],
                "monitor": ["healthmonitor", "create", "--name", app, "--type", "HTTP", "--delay", "10",
                            "--timeout", "5", "--max-retries", "3", "--http-method", "GET",
                            "--http-version", "1.1", "--domain-name", request["hostname"],
                            "--url-path", request["health_path"], "--expected-codes", "200", "--enable", pool],
                "policy": ["l7policy", "create", "--name", app, "--action", "REDIRECT_TO_POOL",
                           "--redirect-pool", pool, "--disable", listener],
                "rule": ["l7rule", "create", "--type", "HOST_NAME", "--compare-type", "EQUAL_TO",
                         "--value", request["hostname"], "--enable", policy],
            }
            record["steps"][kind] = {"intent": True}
            save(path, record)
            try:
                response = call([*commands[kind], "--tag", tag, "--wait", "-f", "json"])
                resource_id = response.get("id") if isinstance(response, dict) else None
            except RouteError:
                resource_id = None
            if not resource_id:
                resource_id = discover(kind)
                if not resource_id:
                    raise RouteError("creation_unresolved_" + kind, "unknown")
        require(is_uuid(resource_id), "invalid_resource_id")
        record["steps"][kind]["id"] = resource_id
        save(path, record)
        verified(kind, resource_id)

    ids = {kind: record["steps"][kind]["id"] for kind in KINDS}
    pool, policy = verified("pool", ids["pool"]), verified("policy", ids["policy"])
    require(relation_ids(pool.get("members")) == {ids["member"]} and
            pool.get("healthmonitor_id") == ids["monitor"], "unexpected_pool_resources")
    require(relation_ids(policy.get("rules")) == {ids["rule"]}, "unexpected_policy_rules")
    if not policy["admin_state_up"]:
        if record.get("activation_intent"):
            raise RouteError("activation_unresolved", "unknown")
        preflight()
        record["activation_intent"] = True
        save(path, record)
        try:
            call(["l7policy", "set", "--enable", "--wait", ids["policy"]])
        except RouteError:
            pass
        if not verified("policy", ids["policy"])["admin_state_up"]:
            raise RouteError("activation_unresolved", "unknown")
    preflight()
    record["status"] = "configured"
    save(path, record)
    return {**request, "status": "configured", "https_verified": False, "resources": ids,
            "network_rule_id": network_rule_id}


def main():
    try:
        require(os.geteuid() == ROOT_UID, "root_required")
        require(len(sys.argv) == 1, "arguments_not_supported")
        config = read_private(CONFIG)
        raw = sys.stdin.read(16385)
        require(len(raw) <= 16384, "request_too_large")
        result = configure(config, json.loads(raw))
    except RouteError as error:
        result = {"status": error.status, "reason": str(error), "https_verified": False}
    except Exception:
        # Includes corrupt private state and native errors; no traceback/CLI data.
        result = {"status": "blocked", "reason": "invalid_local_state", "https_verified": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "configured" else 1


if __name__ == "__main__":
    sys.exit(main())
