"""Explicit operator commands; never provision at import time."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .state import StateStore, atomic_private_write, private_directory, read_private
from .report import installation_report
from .preflight import RetiredEnrollment, load_config, parse_args, validate_config

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


def prompt_configuration(path, args):
    print('고객 노드의 OpenStack 설정을 입력하세요. 인증 비밀값은 다음 단계에서 숨겨 입력합니다.')
    config = {
        'openstack': {
            'auth_url': input('Keystone HTTPS v3 URL: ').strip(),
            'project_id': args.project_id or input('프로젝트 ID: ').strip(),
            'user_id': args.user_id or input('사용자 ID: ').strip(),
        },
        'vm_access': {
            'network_id': input('VM 관리 네트워크 ID: ').strip(),
            'ssh_source_cidr': input('SSH 접근 허용 CIDR (예: 192.0.2.10/32): ').strip(),
            'ssh_username': input('VM SSH 사용자 [ubuntu]: ').strip() or 'ubuntu',
            'resource_prefix': input('자원 이름 접두사 [jasmin]: ').strip() or 'jasmin',
        },
    }
    validate_config(config)
    private_directory(path.parent)
    atomic_private_write(path, json.dumps(config, indent=2).encode())
    return config


def entered_auth(config, requested_type=None):
    os_config = config['openstack']
    method = requested_type or input('OpenStack 인증 형식 [token/application_credential]: ').strip()
    if method not in ('token', 'application_credential'):
        raise ValueError('지원하는 인증 형식은 token 또는 application_credential입니다.')
    auth = {'auth_url': os_config['auth_url'], 'project_id': os_config['project_id']}
    if method == 'token':
        auth['token'] = getpass.getpass('프로젝트 범위 토큰: ')
        if not auth['token'] or any(character.isspace() for character in auth['token']):
            raise ValueError('프로젝트 범위 토큰을 확인하세요.')
    else:
        auth['application_credential_id'] = input('Application Credential ID: ').strip()
        auth['application_credential_secret'] = getpass.getpass('Application Credential secret: ')
        if not auth['application_credential_id'] or not auth['application_credential_secret']:
            raise ValueError('Application Credential ID와 secret을 확인하세요.')
    for option in ('region_name', 'interface', 'cacert'):
        if os_config.get(option):
            auth[option] = os_config[option]
    return auth


def run_runtime(input_path):
    script = ROOT / 'deployment/scripts/deploy.sh'
    if not script.is_file():
        raise FileNotFoundError('동봉된 앱 배포 스크립트가 없습니다.')
    subprocess.run(['bash', str(script), '--input', str(input_path.resolve())], check=True)


def initialize(args):
    from .credentials import CredentialStore
    from .preflight import run_preflight
    from infrastructure.providers.openstack.cli import OpenStackCLI
    from infrastructure.providers.openstack.discovery import discover_capabilities
    from infrastructure.providers.openstack.access import prepare_vm_access

    config = load_config(args.config) if args.config.exists() else prompt_configuration(args.config, args)
    if ((args.project_id and args.project_id != config['openstack']['project_id'])
            or (args.user_id and args.user_id != config['openstack']['user_id'])):
        raise ValueError('명령의 프로젝트·사용자 ID와 기존 설정이 다릅니다.')
    state = StateStore(args.state_dir / "bootstrap-state.json")
    state.start("configuration")
    binding = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    if state.data.get("config_binding", binding) != binding:
        raise ValueError("Installation configuration changed; explicit migration required")
    state.data["config_binding"] = binding
    state.complete("configuration", {"bound": True})
    state.start("preflight")
    state.complete("preflight", run_preflight())
    state.start("identity")
    vault = CredentialStore(args.config_dir)
    if vault.path.exists():
        auth = vault.load()
        stored_type = 'application_credential' if 'application_credential_id' in auth else 'token' if 'token' in auth else None
        if args.auth_type and args.auth_type != stored_type:
            raise ValueError('기존 로컬 인증 형식과 명령의 인증 형식이 다릅니다.')
    else:
        auth = entered_auth(config, args.auth_type)
    cli = OpenStackCLI(auth)
    issued = cli.run(["token", "issue"])
    if not isinstance(issued, dict) or issued.get('project_id') != config['openstack']['project_id'] or issued.get('user_id') != config['openstack']['user_id']:
        raise ValueError('OpenStack 토큰의 프로젝트 또는 사용자가 입력한 ID와 다릅니다.')
    if not vault.path.exists():
        vault.save(auth)
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
    from .credentials import CredentialStore
    from infrastructure.providers.openstack.cli import OpenStackCLI
    from infrastructure.providers.openstack.discovery import discover_capabilities
    print(json.dumps(discover_capabilities(OpenStackCLI(CredentialStore(args.config_dir).load())), indent=2))


def uninstall(args):
    from .wireguard import uninstall_wireguard
    installation_path = args.config_dir / "installation.json"
    installation = json.loads(read_private(installation_path)) if installation_path.exists() else {}
    result = (uninstall_wireguard(Path(installation["wireguard_config_path"]))
              if "wireguard_config_path" in installation else {"removed": False})
    # Keep cloud resource ownership and credentials for explicit operator revocation.
    print(json.dumps({"wireguard": result, "retained": [str(args.config_dir), str(args.state_dir)],
                      "next_action": "Revoke any previous server registration and OpenStack credential, then explicitly remove retained local files."}))


def require_root():
    if os.geteuid() != 0:
        raise PermissionError("root execution required")


def main(argv=None):
    args = parse_args(argv)
    os.environ.pop("JASMIN_ENROLLMENT_TOKEN", None)
    os.environ.pop("RAILSHOT_ENROLLMENT_TOKEN", None)
    failure_stage = "configuration"
    try:
        require_root()
        private_directory(args.config_dir)
        with installation_lock(args.state_dir):
            if args.command == "init":
                try:
                    initialize(args)
                    if args.runtime_input is not None:
                        runtime_state = StateStore(args.state_dir / "bootstrap-state.json")
                        runtime_state.start("runtime_deploy")
                        run_runtime(args.runtime_input)
                        runtime_state.complete("runtime_deploy", {"executed": True})
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
        if isinstance(exc, RetiredEnrollment):
            print(str(exc), file=sys.stderr)
        # Exception text may contain HTTP responses, command output, or credentials.
        print(f"실패 단계={failure_stage}, 오류 유형={type(exc).__name__}. docs/architecture/client-bootstrap.md의 단계별 복구 안내를 확인하십시오. 원본 오류는 비밀정보 보호를 위해 숨깁니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
