"""Pinned SSH forwarding through native cloud CLIs; no user-supplied commands."""
from contextlib import contextmanager
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time


def transport_parts(reference):
    if not isinstance(reference, str):
        raise ValueError('transport_ref must name SSM or IAP')
    match = re.fullmatch(r'ssm:([a-z]{2}(?:-[a-z]+)+-\d):(i-[a-f0-9]{8,17})', reference)
    if match:
        return ('ssm', *match.groups())
    match = re.fullmatch(r'iap:([a-z][a-z0-9-]{4,61}[a-z0-9])/([a-z]+-[a-z]+[1-9][0-9]*-[a-z])/([a-z][a-z0-9-]{0,61}[a-z0-9])', reference)
    if match:
        return ('iap', *match.groups())
    raise ValueError('transport_ref must name a concrete SSM instance or IAP instance')


def tunnel_command(reference, port):
    kind, *parts = transport_parts(reference)
    if kind == 'ssm':
        region, instance = parts
        return ['aws', 'ssm', 'start-session', '--region', region, '--target', instance,
                '--document-name', 'AWS-StartPortForwardingSession', '--parameters',
                json.dumps({'portNumber': ['22'], 'localPortNumber': [str(port)]})]
    project, zone, instance = parts
    return ['gcloud', 'compute', 'start-iap-tunnel', instance, '22', '--project', project,
            '--zone', zone, '--local-host-port', f'127.0.0.1:{port}', '--quiet']


@contextmanager
def forwarded_port(reference, deadline):
    if reference is None:
        yield None
        return
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    command = tunnel_command(reference, port)
    executable = shutil.which(command[0])
    if executable is None or (command[0] == 'aws' and shutil.which('session-manager-plugin') is None):
        raise ValueError('Native cloud tunnel executable is unavailable')
    command[0] = executable
    # Cloud CLI credentials stay in this controller child, never in Ansible or the guest.
    env = dict(os.environ, AWS_PAGER='', CLOUDSDK_CORE_DISABLE_PROMPTS='1')
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=env, start_new_session=True)
        try:
            ready_by = min(deadline, time.monotonic() + 60)
            while True:
                if process.poll() is not None:
                    raise ValueError('Cloud tunnel exited before SSH forwarding was ready')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= ready_by:
                        raise ValueError('Cloud SSH forwarding readiness timed out') from None
                    time.sleep(0.2)
            yield port
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            finally:
                # A finished CLI parent can still leave a forwarding child in its group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
