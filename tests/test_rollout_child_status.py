import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kilix_rollout import child_status, launch
from kilix_rollout.model import Session


class StatusTests(unittest.TestCase):
    def test_missing_ack_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            start = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'no child startup acknowledgment'):
                child_status.wait_started(Path(tmp)/'missing', timeout=.1)
            self.assertLess(time.monotonic()-start, 1)

    def test_spawn_failure_and_normal_exit_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp)/'status.json'
            self.assertEqual(child_status.main([str(receipt), '--', '/no/such/program']), 127)
            with self.assertRaisesRegex(RuntimeError, 'failed during startup'):
                child_status.wait_started(receipt)
            self.assertEqual(child_status.main([str(receipt), '--', sys.executable, '-c', 'pass']), 0)
            self.assertEqual(child_status.wait_started(receipt)['stage'], 'exited')
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('command', json.loads(receipt.read_text()))

    def test_private_receipts_do_not_change_child_file_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = os.umask(0o022)
            try:
                self.assertEqual(child_status.main([str(root/'status.json'), '--', sys.executable,
                    '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("ok")',
                    str(root/'result')]), 0)
            finally:
                os.umask(previous)
            self.assertEqual((root/'result').stat().st_mode & 0o777, 0o644)
            self.assertEqual((root/'status.json').stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(shutil.which('tmux'), 'tmux required')
    def test_private_tmux_early_error_and_startup_ack(self):
        with tempfile.TemporaryDirectory(prefix='rollout-start-') as tmp:
            root = Path(tmp); socket = root/'socket'
            tmux = root/'tmux';tmux.write_text(f'#!/bin/sh\nexec /usr/bin/tmux -L unused -S {socket} "$@"\n');tmux.chmod(0o700)
            exe = root/'provider';exe.write_text('#!/bin/sh\nexit 23\n');exe.chmod(0o700)
            item = Session(provider='codex',session_id='fixture',path='/tmp/transcript',cwd=tmp,title='test',updated=0,state='idle')
            with mock.patch.dict(os.environ, {'KILIX_ROLLOUT_RESUME_HOME': str(root/'config')}), \
                 mock.patch.object(launch.config,'configured_program',side_effect=lambda key,default: str(tmux) if key=='tmux' else default):
                try:
                    report={};start=time.monotonic()
                    with self.assertRaisesRegex(RuntimeError,'exit 23'):
                        launch.start_detached(item,executable=str(exe),startup_status=report)
                    self.assertLess(time.monotonic()-start, 3)
                    self.assertEqual(json.loads(Path(report['status_file']).read_text())['exit'],23)
                    exe.write_text('#!/bin/sh\nsleep 1\nexit 0\n')
                    report={};name=launch.start_detached(item,executable=str(exe),startup_status=report)
                    self.assertEqual(report['startup']['stage'],'started')
                    self.assertTrue(name)
                    deadline=time.monotonic()+3
                    while json.loads(Path(report['status_file']).read_text())['stage']!='exited' and time.monotonic()<deadline:time.sleep(.05)
                    self.assertEqual(json.loads(Path(report['status_file']).read_text())['exit'],0)
                finally:
                    subprocess.run([str(tmux),'kill-server'],capture_output=True)
