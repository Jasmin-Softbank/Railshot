import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


client = module('personal_test_client', 'apps/agent/personal.py')
remove = module('personal_test_remove', 'apps/agent/personal_remove.py')
gateway = module('personal_test_gateway', 'deployment/scripts/personal_wireguard.py')
KEY = base64.b64encode(bytes(range(32))).decode()
KEY2 = base64.b64encode(bytes(range(1, 33))).decode()


@pytest.fixture
def local(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    for name in ('CONFIG', 'STATE', 'PAYLOAD'):
        directory = tmp_path / name.lower()
        directory.mkdir(mode=0o700)
        monkeypatch.setattr(client, name, directory)
        monkeypatch.setattr(remove, name, directory)
    for name in ('UNIT', 'RUNTIME_UNIT', 'WG'):
        path = tmp_path / name.lower()
        monkeypatch.setattr(client, name, path)
        monkeypatch.setattr(remove, name, path)
    monkeypatch.setattr(remove, 'CLI_HOME', tmp_path / 'cli-home')
    return tmp_path


def configuration():
    return {'target_id': 'target1', 'generation': 1, 'client_token': 'secret', 'api_url': 'https://example.test',
            'tunnel': {'server_public_key': KEY, 'endpoint': 'example.test:51820',
                       'address': '10.99.0.2/32', 'allowed_ips': '10.99.0.1/32'}}


def test_tunnel_rejects_transit_route():
    config = configuration()['tunnel']
    assert client.validate_tunnel(config) == ('10.99.0.2/32', '10.99.0.1/32')
    for route in ('0.0.0.0/0', '10.99.0.0/24', '10.99.0.2/32'):
        with pytest.raises(ValueError):
            client.validate_tunnel({**config, 'allowed_ips': route})


@pytest.mark.parametrize('url', ['http://example.test', 'https://user:pass@example.test', 'https://example.test/?token=x', 'https://example.test/#x'])
def test_no_credentials_redirect_or_plaintext_url(url):
    with pytest.raises(ValueError):
        client.https_url(url)


def test_removal_rejects_nonempty_applications(local):
    with pytest.raises(ValueError, match='APPLICATIONS_NOT_REMOVED'):
        client.handle_command(configuration(), {'id': 'operation1', 'kind': 'environment.delete', 'applications': ['app1'], 'delete_data': True})
    assert list(client.STATE.iterdir()) == []


def test_command_is_durable_and_not_replayed_after_unknown(local):
    receipts, launches = [], []
    command = {'id': 'operation1', 'kind': 'environment.delete', 'applications': [], 'delete_data': True}
    config = configuration()
    client.handle_command(config, command, sender=lambda c, r: receipts.append(r), launch=lambda *a: launches.append(a))
    client.handle_command(config, command, sender=lambda c, r: receipts.append(r), launch=lambda *a: launches.append(a))
    assert len(launches) == 1
    assert [r['status'] for r in receipts] == ['running', 'unknown']
    assert all(not r['client_removed'] for r in receipts)
    with pytest.raises(ValueError, match='JOB_ID_CONFLICT'):
        client.handle_command(config, {**command, 'delete_data': False})


def test_wrong_generation_does_not_write(local):
    with pytest.raises(ValueError, match='GENERATION_MISMATCH'):
        client.handle_command(configuration(), {'id': 'op', 'kind': 'environment.delete', 'applications': [], 'delete_data': True, 'generation': 2})
    assert list(client.STATE.iterdir()) == []


def prepare_owned():
    config = configuration()
    (remove.PAYLOAD / '.railshot-personal.json').write_text('{"version":1}')
    (remove.CONFIG / 'client.json').write_text(json.dumps(config))
    for path in (remove.UNIT, remove.WG):
        path.write_text(remove.MARKER + 'owned')
    return config


def test_removal_never_adopts_foreign_file(local):
    config = prepare_owned()
    (remove.CONFIG / 'customer-secret').write_text('keep')
    commands = []
    with pytest.raises(ValueError):
        remove.cleanup(config, runner=lambda args: commands.append(args))
    assert commands == []
    assert (remove.CONFIG / 'customer-secret').exists()


def test_removal_verifies_tunnel_and_preserves_unowned_sibling(local):
    config = prepare_owned()
    customer = local / 'customer-service'
    customer.write_text('keep')
    commands = []
    def runner(args):
        commands.append(args)
        return 'inactive' if 'show' in args and 'systemctl' in args else ''
    steps = remove.cleanup(config, runner=runner)
    assert len(steps) == 3
    assert customer.read_text() == 'keep'
    assert not remove.PAYLOAD.exists() and not remove.CONFIG.exists() and not remove.WG.exists()
    assert not any('openstack' in args or 'k3s' in args for args in commands)


def test_failed_tunnel_stop_is_not_success(local):
    config = prepare_owned()
    def runner(args):
        return 'inactive' if args[0] == 'systemctl' and 'show' in args else 'railshot0'
    with pytest.raises(ValueError):
        remove.cleanup(config, runner=runner)
    assert remove.PAYLOAD.exists()


def gateway_config(tmp_path):
    secret = tmp_path / 'key'
    secret.write_text(KEY)
    secret.chmod(0o600)
    return {'version': 1, 'interface': 'railshotwg', 'address_pool': '10.99.0.0/24',
            'server_address': '10.99.0.1/32', 'endpoint': 'example.test:51820', 'listen_port': 51820,
            'private_key_file': str(secret), 'ledger_path': str(tmp_path / 'ledger.json')}


def request(action='register', target='target1', public=KEY):
    return {'action': action, 'target_id': target, 'generation': 1, 'public_key': public}


def fake_gateway(args, **kwargs):
    return KEY if args[1] == 'pubkey' else KEY + '\t' + str(int(time.time()))


def test_gateway_idempotent_isolated_and_revoked(tmp_path):
    config = gateway_config(tmp_path)
    apply = lambda *args: None
    first = gateway.execute(config, request(), fake_gateway, apply)
    repeated = gateway.execute(config, request(), fake_gateway, apply)
    second = gateway.execute(config, request(target='target2', public=KEY2), fake_gateway, apply)
    assert first == repeated
    assert first['tunnel']['address'] != second['tunnel']['address']
    assert first['tunnel']['allowed_ips'] == '10.99.0.1/32'
    gateway.execute(config, request('remove'), fake_gateway, apply)
    with pytest.raises(ValueError, match='GATEWAY_PEER_REVOKED'):
        gateway.execute(config, request(), fake_gateway, apply)
    with pytest.raises(ValueError, match='GATEWAY_KEY_REUSED'):
        gateway.execute(config, request(target='other'), fake_gateway, apply)


def test_gateway_failure_keeps_reservation_for_reconciliation(tmp_path):
    config = gateway_config(tmp_path)
    def fail(*args):
        raise RuntimeError('network error')
    with pytest.raises(RuntimeError):
        gateway.execute(config, request(), fake_gateway, fail)
    before = json.loads(Path(config['ledger_path']).read_text())
    result = gateway.execute(config, request(), fake_gateway, lambda *a: None)
    assert result['tunnel']['address'] == before['target1:1']['address']


def test_gateway_stale_handshake_is_not_connected(tmp_path):
    config = gateway_config(tmp_path)
    def runner(args, **kwargs):
        return KEY if args[1] == 'pubkey' else KEY + '\t1'
    result = gateway.execute(config, request(), runner, lambda *a: None)
    assert result['reachable'] is False


def test_heartbeat_no_false_health_on_command_failure(local):
    config = configuration()
    config['kubeconfig'] = '/missing'
    def fail(*args, **kwargs):
        raise RuntimeError()
    assert client.checks(config, runner=fail, cli_factory=fail) == {'tunnel': False, 'openstack': False, 'runtime': False}


def test_gateway_apply_adds_peer_routes_and_blocks_transit(tmp_path, monkeypatch):
    config = gateway_config(tmp_path)
    peers = {'live': {'public_key': KEY2, 'address': '10.99.0.2/32', 'removed': False},
             'gone': {'public_key': KEY, 'address': '10.99.0.3/32', 'removed': True}}
    saved, commands = [], []
    monkeypatch.setattr(gateway, 'save', lambda path, text: saved.append(text))
    def runner(args, **kwargs):
        commands.append(args)
        if args[:4] == ['ip', '-4', 'route', 'show']:
            return '10.99.0.3 dev railshotwg'
        return ''
    gateway.apply(config, peers, runner)
    assert ['ip', '-4', 'route', 'replace', '10.99.0.2/32', 'dev', 'railshotwg'] in commands
    assert ['ip', '-4', 'route', 'del', '10.99.0.3/32', 'dev', 'railshotwg'] in commands
    assert 'PostUp = iptables' in saved[0]
    assert 'AllowedIPs = 10.99.0.2/32' in saved[0]
    assert 'AllowedIPs = 10.99.0.3/32' not in saved[0]


def test_metrics_measure_only_local_samples_and_never_invent_missing(tmp_path):
    from types import SimpleNamespace
    (tmp_path / 'net').mkdir()
    (tmp_path / 'stat').write_text('cpu 10 0 10 80 0 0 0 0\n')
    (tmp_path / 'meminfo').write_text('MemTotal: 100 kB\nMemAvailable: 40 kB\n')
    (tmp_path / 'net/dev').write_text('header\nheader\neth0: 1000 0 0 0 0 0 0 0 500 0 0 0 0 0 0 0\n')
    timestamps = iter([1, 11, 21])
    metrics = client.HostMetrics(tmp_path, clock=lambda: next(timestamps), disk=lambda _: SimpleNamespace(total=100, used=25))
    first = metrics.collect()
    assert first['cpu_percent'] is None and first['network_receive_bytes_per_second'] is None
    assert first['memory_percent'] == 60 and first['disk_percent'] == 25
    (tmp_path / 'stat').write_text('cpu 20 0 20 160 0 0 0 0\n')
    (tmp_path / 'net/dev').write_text('header\nheader\neth0: 2000 0 0 0 0 0 0 0 1000 0 0 0 0 0 0 0\n')
    second = metrics.collect()
    assert second['cpu_percent'] == 20 and second['network_receive_bytes_per_second'] == 100
    (tmp_path / 'meminfo').unlink()
    assert metrics.collect()['memory_percent'] is None


def test_release_package_is_deterministic_and_contains_no_tests_or_keys(tmp_path):
    import tarfile
    packaging = module('personal_test_packaging', 'deployment/scripts/package-personal-client.py')
    first = packaging.package(ROOT, tmp_path / 'one.tgz')
    second = packaging.package(ROOT, tmp_path / 'two.tgz')
    assert first['artifact_sha256'] == second['artifact_sha256']
    with tarfile.open(first['artifact_path']) as archive:
        names = archive.getnames()
        assert 'apps/agent/personal_remove.py' in names and 'apps/agent/openstack_control.py' in names
        assert not any('tests/' in name or name.endswith('.key') for name in names)
    with pytest.raises(FileExistsError):
        packaging.package(ROOT, tmp_path / 'one.tgz')


def test_cli_access_denies_root_shell_and_uses_dedicated_account(local, monkeypatch):
    from types import SimpleNamespace
    blob = base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes(range(32))).decode()
    config = configuration()
    config['runtime_access'] = {'public_key': 'ssh-ed25519 ' + blob}
    home = local / 'unprivileged-cli'
    home.mkdir(mode=0o700)
    monkeypatch.setattr(client, 'CLI_HOME', home)
    monkeypatch.setattr(client, 'install_cli_account', lambda _: None)
    monkeypatch.setattr(client.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid()))
    commands = []
    monkeypatch.setattr(client, 'run', lambda args: commands.append(args) or '')
    client.install_runtime_access(config)
    config_text = (client.CONFIG / 'runtime_sshd_config').read_text()
    assert 'PermitRootLogin no' in config_text
    assert 'AllowUsers railshot-openstack' in config_text
    assert 'ForceCommand ' in config_text and 'openstack_control.py' in config_text
    assert 'k3s' not in config_text and 'PermitTTY no' in config_text
    assert str(home / 'authorized_keys') in config_text
    assert 'restrict,from="10.99.0.1/32"' in (home / 'authorized_keys').read_text()


def test_install_success_waits_for_server_control_verification():
    config = {**configuration(), 'project_id': 'project1'}
    calls = []
    statuses = iter(['connecting', 'ready'])
    client.wait_control_ready(config, transport=lambda *a: calls.append(a) or {'status': next(statuses)},
                              checker=lambda _: {'tunnel': True, 'openstack': True, 'runtime': True}, sleeper=lambda _: None)
    assert len(calls) == 2
    with pytest.raises(ValueError, match='CONTROL_CONNECTION_FAILED'):
        client.wait_control_ready(config, transport=lambda *a: {'status': 'attention'}, checker=lambda _: {}, sleeper=lambda _: None)


def test_removal_cleans_only_recorded_cli_account(local, monkeypatch):
    from types import SimpleNamespace
    config = prepare_owned()
    home = remove.CLI_HOME
    home.mkdir(mode=0o700)
    (home / 'control.json').write_text('{}')
    account = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid(), pw_dir=str(home))
    monkeypatch.setattr(remove.pwd, 'getpwnam', lambda _: account)
    (remove.CONFIG / 'cli-account.json').write_text(json.dumps({'user': remove.CLI_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}))
    commands = []
    def runner(args):
        commands.append(args)
        return 'inactive' if args[0] == 'systemctl' and 'show' in args else ''
    remove.cleanup(config, runner=runner)
    assert ['userdel', 'railshot-openstack'] in commands
    assert not home.exists()
