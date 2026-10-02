"""Native local process groups only; no cloud CLI or guest connections."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import transport


class TunnelCleanupTests(unittest.TestCase):
    def test_parent_exit_and_sigterm_resistance_never_leave_a_tunnel_child(self):
        child_code = '''import os,signal,socket,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sys.argv[3] != 'early-exit':
    listener=socket.socket(); listener.bind(('127.0.0.1',int(sys.argv[1]))); listener.listen()
Path(sys.argv[2]).write_text(str(os.getpid()))
time.sleep(60)
'''
        parent_code = '''import signal,subprocess,sys,time
from pathlib import Path
if sys.argv[4] == 'ignore-term': signal.signal(signal.SIGTERM,signal.SIG_IGN)
subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:]])
while not Path(sys.argv[3]).exists(): time.sleep(.01)
if sys.argv[4] != 'early-exit': time.sleep(60)
'''
        native_popen = subprocess.Popen
        for mode in ('early-exit', 'parent-term', 'ignore-term', 'body-error'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                pid_file = Path(directory) / 'child.pid'
                processes = []
                def launch(*args, **kwargs):
                    process = native_popen(*args, **kwargs)
                    processes.append(process)
                    return process
                def command(_reference, port):
                    return [sys.executable, '-c', parent_code, child_code, str(port), str(pid_file), mode]
                try:
                    with patch.object(transport, 'tunnel_command', side_effect=command), \
                            patch.object(transport.subprocess, 'Popen', side_effect=launch):
                        if mode == 'early-exit':
                            with self.assertRaisesRegex(ValueError, 'exited before'):
                                with transport.forwarded_port('local-fixture', time.monotonic() + 5):
                                    self.fail('Fixture does not open a listener')
                        elif mode == 'body-error':
                            with self.assertRaisesRegex(RuntimeError, 'caller failed'):
                                with transport.forwarded_port('local-fixture', time.monotonic() + 5):
                                    raise RuntimeError('caller failed')
                        else:
                            with transport.forwarded_port('local-fixture', time.monotonic() + 5):
                                self.assertTrue(pid_file.exists())
                    self.assertEqual(len(processes), 1)
                    self.assertIsNotNone(processes[0].poll())
                    child_pid = pid_file.read_text()
                    deadline = time.monotonic() + 2
                    while True:
                        state = subprocess.run(['ps', '-p', child_pid, '-o', 'stat='],
                                               capture_output=True, text=True, timeout=5).stdout.strip()
                        if not state or state.startswith('Z') or time.monotonic() >= deadline:
                            break
                        time.sleep(.02)
                    self.assertTrue(not state or state.startswith('Z'), 'Fixture child is still running: ' + state)
                finally:
                    # Even a regression must leave no fixture process behind.
                    for process in processes:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
