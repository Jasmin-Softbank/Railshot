import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

spec = importlib.util.spec_from_file_location('slot_worker', Path(__file__).parents[1] / 'personal-slot-worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def test_entry_creates_private_namespace_before_any_mount():
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs)); return SimpleNamespace(returncode=17)
    with patch.object(worker, 'validate'), patch.object(worker.os, 'geteuid', return_value=0), patch.object(worker.os, 'stat', return_value=SimpleNamespace(st_ino=42)):
        assert worker.execute([], run) == 17
    assert len(calls) == 1
    assert calls[0][0][:4] == ['/usr/bin/unshare', '--mount', '--propagation', 'private']
    assert calls[0][0][-2:] == ['--inside', '42']
    assert calls[0][1]['env'] == worker.ENVIRONMENT


def test_same_namespace_cannot_mount():
    with patch.object(worker, 'validate'), patch.object(worker.os, 'geteuid', return_value=0), patch.object(worker.os, 'stat', return_value=SimpleNamespace(st_ino=42)):
        with pytest.raises(ValueError, match='mount_namespace_not_isolated'):
            worker.execute(['--inside', '42'], lambda *a, **kw: pytest.fail('must not mount'))


def test_child_maps_only_fixed_slot_paths_before_worker():
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs)); return SimpleNamespace(returncode=0)
    with patch.object(worker, 'validate'), patch.object(worker.os, 'geteuid', return_value=0), patch.object(worker.os, 'stat', return_value=SimpleNamespace(st_ino=43)):
        assert worker.execute(['--inside', '42'], run) == 0
    assert [x[0] for x in calls[:3]] == [['/usr/bin/mount', '--bind', str(s), str(t)] for s,t in worker.BINDINGS]
    assert calls[-1][0] == ['/usr/bin/python3', '-I', '/opt/railshot/octavia/openstack_route_worker.py']
    assert all(x[1]['env'] == worker.ENVIRONMENT for x in calls)


def test_failed_mount_never_runs_worker():
    calls = []
    def run(command, **kwargs):
        calls.append(command); raise worker.subprocess.CalledProcessError(1, command)
    with patch.object(worker, 'validate'), patch.object(worker.os, 'geteuid', return_value=0), patch.object(worker.os, 'stat', return_value=SimpleNamespace(st_ino=43)):
        with pytest.raises(worker.subprocess.CalledProcessError): worker.execute(['--inside','42'],run)
    assert len(calls) == 1


@pytest.mark.parametrize('arguments', [['/tmp/evil'], ['--inside','bad'], ['--inside','42','more']])
def test_rejects_custom_paths_and_arguments(arguments):
    with patch.object(worker, 'validate'), patch.object(worker.os, 'geteuid', return_value=0):
        with pytest.raises(ValueError, match='invalid_arguments'):worker.execute(arguments)


def test_non_root_rejected_before_path_access():
    with patch.object(worker.os, 'geteuid', return_value=1000), patch.object(worker,'validate',side_effect=AssertionError):
        with pytest.raises(ValueError,match='root_required'):worker.execute([])
