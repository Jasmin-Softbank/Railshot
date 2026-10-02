"""Explicit operator commands; never provision at import time."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit
import json
import os
from pathlib import Path
import sys
import uuid

from .state import StateStore, atomic_private_write, private_directory, read_private
from .credentials import CredentialStore
from .report import installation_report

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


@contextmanager
def installation_lock(directory):
    private_directory(directory)
    path = directory / "install.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        read_private(path)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def load_config(path):
    # Configuration contains no passwords; nevertheless require root ownership/private mode.
    config = json.loads(read_private(path))
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    validate_config(config)
    return config


def validate_config(config):
    for name in ("service_url",):
        parsed = urlsplit(config.get(name, ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Invalid service URL")
    identity = config.get("openstack")
    access = config.get("vm_access")
    if not isinstance(identity, dict) or not isinstance(access, dict):
        raise ValueError("OpenStack and vm_access configurations are required")
    for name in ("auth_url", "user_domain_name", "project_id", "role_id"):
        if not isinstance(identity.get(name), str) or not identity[name].strip():
            raise ValueError("Missing identity configuration")
    endpoint = urlsplit(identity["auth_url"])
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment or not endpoint.path.rstrip("/").endswith("/v3"):
        raise ValueError("Invalid Keystone endpoint")
    for name in ("network_id", "ssh_username"):
        if not isinstance(access.get(name), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", access[name]):
            raise ValueError("Missing VM access configuration")
    if access["ssh_username"] == "root" or ipaddress.ip_network(access.get("ssh_source_cidr", ""), strict=True).prefixlen == 0:
        raise ValueError("Restricted non-root VM access required")


def initialize(args):
    from .preflight import run_preflight
    from .enrollment import enroll_client
    from .wireguard import ensure_keypair, configure_wireguard
    from infrastructure.providers.openstack.cli import OpenStackCLI
    from infrastructure.providers.openstack.identity import configure_identity
    from infrastructure.providers.openstack.discovery import discover_capabilities
    from infrastructure.providers.openstack.access import prepare_vm_access

    config = load_config(args.config)
    state = StateStore(args.state_dir / "bootstrap-state.json")
    state.start("configuration")
    binding = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    if state.data.get("config_binding", binding) != binding:
        raise ValueError("Installation configuration changed; explicit migration required")
    state.data["config_binding"] = binding
    state.complete("configuration", {"bound": True})
    state.start("preflight")
    state.complete("preflight", run_preflight())
    if "request_id" not in state.data:
        state.data["request_id"] = str(uuid.uuid4())
        state.save()
    state.start("registration")
    private_key, public_key = ensure_keypair(args.config_dir / "wireguard")
    registration_path = args.config_dir / "registration.json"
    if registration_path.exists():
        registration = json.loads(read_private(registration_path))
    else:
        token = read_private(args.enrollment_token_file).decode().strip() if args.enrollment_token_file else (args.enrollment_token or getpass.getpass("일회용 서비스 등록 키: "))
        args.enrollment_token = None
        registration = enroll_client(config["service_url"], token, public_key, state.data["request_id"])
        atomic_private_write(registration_path, json.dumps(registration).encode())
        del token
    state.complete("registration", {"node_id": registration["node_id"]})
    wg_path = Path(config.get("wireguard_config_path", "/etc/wireguard/jasmin0.conf"))
    atomic_private_write(args.config_dir / "installation.json", json.dumps({"wireguard_config_path": str(wg_path)}).encode())
    state.start("wireguard_configuration")
    state.complete("wireguard_configuration", configure_wireguard(registration, private_key, wg_path))
    del private_key
    # configure_wireguard returns only after its authenticated tunnel probe succeeds.
    state.complete("wireguard_connectivity", {"connected": True})
    state.start("identity")
    vault = CredentialStore(args.config_dir)
    os_config = dict(config["openstack"])
    os_config["existing_resources"] = state.data["resources"]
    if vault.path.exists():
        auth = vault.load()
    else:
        if not os_config.get("service_username"):
            os_config["service_username"] = input("OpenStack 전용 계정 이름: ").strip()
        if not os_config.get("admin_username"):
            os_config["admin_username"] = input("OpenStack 관리자 ID: ").strip()
        password = getpass.getpass("OpenStack 관리자 암호: ")
        auth = configure_identity(os_config, password, on_resource=state.record_resource)
        del password
        vault.save(auth)
    cli = OpenStackCLI(auth)
    cli.run(["token", "issue"])
    state.complete("identity", {"verified": True})
    state.start("discovery")
    capabilities = discover_capabilities(cli)
    state.complete("discovery", capabilities)
    access_config = dict(config["vm_access"])
    access_config["existing_resources"] = state.data["resources"]
    state.start("vm_access_preparation")
    access = prepare_vm_access(cli, access_config, args.state_dir, on_resource=state.record_resource)
    atomic_private_write(args.state_dir / "vm-access.json", json.dumps(access, indent=2).encode())
    state.complete("vm_access_preparation", {"prepared": True, "connectivity_verified": False})
    report = installation_report(state.data, capabilities, access)
    atomic_private_write(args.state_dir / "report.json", json.dumps(report, indent=2).encode())
    print(json.dumps(report, indent=2, ensure_ascii=False))


def diagnose(args):
    from infrastructure.providers.openstack.cli import OpenStackCLI
    from infrastructure.providers.openstack.discovery import discover_capabilities
    print(json.dumps(discover_capabilities(OpenStackCLI(CredentialStore(args.config_dir).load())), indent=2))


def uninstall(args):
    from .wireguard import uninstall_wireguard
    installation = json.loads(read_private(args.config_dir / "installation.json"))
    result = uninstall_wireguard(Path(installation["wireguard_config_path"]))
    # Keep cloud resource ownership and credentials for explicit operator revocation.
    print(json.dumps({"wireguard": result, "retained": [str(args.config_dir), str(args.state_dir)],
                      "next_action": "Revoke server registration and OpenStack credential, then explicitly remove retained local files."}))


def require_root():
    if os.geteuid() != 0:
        raise PermissionError("root execution required")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Railshot 고객 OpenStack 설치·진단")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/jasmin/bootstrap"))
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/jasmin"))
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--config", type=Path, required=True)
    init.add_argument("--enrollment-token-file", type=Path)
    commands.add_parser("diagnose")
    commands.add_parser("uninstall")
    verify = commands.add_parser("verify-vm")
    verify.add_argument("--profile", type=Path, required=True)
    verify.add_argument("--known-hosts", type=Path, required=True)
    args = parser.parse_args(argv)
    legacy_token = os.environ.pop("JASMIN_ENROLLMENT_TOKEN", None)
    args.enrollment_token = os.environ.pop("RAILSHOT_ENROLLMENT_TOKEN", None) or legacy_token
    failure_stage = "configuration"
    try:
        require_root()
        private_directory(args.config_dir)
        with installation_lock(args.state_dir):
            if args.command == "init":
                try:
                    initialize(args)
                except Exception as exc:
                    try:
                        failed_state = StateStore(args.state_dir / "bootstrap-state.json")
                        failure_stage = failed_state.fail_current(type(exc).__name__)
                    except Exception:
                        pass
                    raise
            elif args.command == "diagnose":
                diagnose(args)
            elif args.command == "uninstall":
                uninstall(args)
            else:
                from infrastructure.providers.openstack.access import verify_vm_access
                print(json.dumps(verify_vm_access(json.loads(read_private(args.profile)), args.known_hosts), indent=2))
    except Exception as exc:
        # Exception text may contain HTTP responses, command output, or credentials.
        print(f"실패 단계={failure_stage}, 오류 유형={type(exc).__name__}. docs/architecture/client-bootstrap.md의 단계별 복구 안내를 확인하십시오. 원본 오류는 비밀정보 보호를 위해 숨깁니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
