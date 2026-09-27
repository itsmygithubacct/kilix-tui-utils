"""Real private host-query processes and selected owned-backend handoff."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_music import music
from test_music_sources import backend_fixture, wait_until
from kilix_tui import app
from kilix_tui import music_protocol


class SelectedBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.host=self.base/'host';(self.host/'scripts').mkdir(parents=True)
        (self.host/'kilix').write_text('#!/bin/sh\nexit 99\n')
        (self.host/'kilix').chmod(0o700)
        self.executable=backend_fixture(self.base)
        self.calls=self.base/'host-calls'
        self.reply=self.base/'reply'
        self.helper=self.host/'scripts/install-kilix-amp.py'
        self.helper.write_text('import json,os,sys\n'
            +f'with open({str(self.calls)!r},"a") as log: log.write(json.dumps(sys.argv[1:])+"\\n")\n'
            +f'print(open({str(self.reply)!r}).read())\n')
        self.root=str(self.base/"95 apps 'quoted'")
        self.record={'id':'kilix-amp','root':self.root,'ref':'1'*40,
            'build':['make','ENCODEC=1','all'],'executable':str(self.executable)}
        self.reply.write_text(json.dumps(self.record))
        env={'PATH':os.defpath,'HOME':str(self.base),'KILIX_HOME':str(self.host),
             'KILIX_CONTENT_ROOT':self.root,'PYTHONDONTWRITEBYTECODE':'1'}
        self.env=patch.dict(os.environ,env,clear=True);self.env.start();self.addCleanup(self.env.stop)

    def test_path_and_development_executables_never_replace_catalog_selection(self):
        fake=self.base/'bin';fake.mkdir()
        (fake/'kilix-amp').write_text('#!/bin/sh\nexit 99\n');(fake/'kilix-amp').chmod(0o700)
        dev=self.base/'sources/kilix-apps/kilix-amp';dev.mkdir(parents=True)
        (dev/'kilix-amp').write_text('#!/bin/sh\nexit 99\n');(dev/'kilix-amp').chmod(0o700)
        with patch.dict(os.environ,{'PATH':str(fake),'GPU_TERMINAL_SOURCE_HOME':str(self.base/'sources')}):
            self.assertEqual(music.backend_executable(),str(self.executable))
            self.reply.write_text(json.dumps(dict(self.record,executable=None)))
            self.assertEqual(music.backend_executable(),'')
        self.assertFalse(Path(self.root).exists(),'query must not create the selected root')

    def test_owned_start_forwards_exact_root_and_verifies_pid(self):
        # The actual child records the environment before serving its socket.
        source=self.executable.read_text().replace('server = socket.socket',
            f'open({str(self.base/"observed-root")!r},"w").write(os.environ["KILIX_CONTENT_ROOT"])\nserver = socket.socket')
        self.executable.write_text(source)
        backend=music.Backend(str(self.base/'control'))
        try:
            self.assertTrue(backend.start(),backend.error)
            self.assertEqual(backend.identity[-1],backend._owned.pid)
            self.assertEqual((self.base/'observed-root').read_text(),self.root)
            argv=json.loads(self.calls.read_text().splitlines()[0])
            self.assertEqual(argv,['--resolve','--content-root',self.root])
            self.assertEqual(backend.selection['build'],['make','ENCODEC=1','all'])
        finally:backend.close()
        self.assertIsNone(backend._owned)

    def test_install_uses_the_explicit_embedding_root_and_stays_off_render(self):
        self.assertTrue(music.install_backend(root=self.root))
        self.assertEqual(json.loads(self.calls.read_text()),['--json','--content-root',self.root])
        self.assertTrue(music.install_backend())
        self.assertEqual(json.loads(self.calls.read_text().splitlines()[-1]),
                         ['--json','--content-root',self.root])
        before=self.calls.read_bytes()
        with patch.object(music,'_host_selection',side_effect=AssertionError('render query')):
            state=music.State()
            app.render_to_text(music.render,state)
            state.close()
        self.assertEqual(self.calls.read_bytes(),before)

    def test_override_is_explicit_and_never_claims_catalog_identity(self):
        with patch.dict(os.environ,{'KILIX_AMP':str(self.executable)}), \
             patch.object(music,'_host_selection',side_effect=AssertionError('override query')):
            result=music.backend_selection(root=self.root)
        self.assertEqual(result,{'executable':str(self.executable),'root':self.root,'override':True})
        with patch.dict(os.environ,{'KILIX_AMP':str(self.base/'missing')}):
            self.assertEqual(music.backend_executable(),'')
        self.assertFalse(self.calls.exists())

    def test_missing_relative_and_changed_root_selections_refuse_before_start(self):
        for change in ({'root':str(self.base/'other')},{'root':'relative'}, {'root':None},
                       {'id':'other'},{'ref':'main'},{'build':[],'executable':'relative'}):
            with self.subTest(change=change):
                self.reply.write_text(json.dumps(dict(self.record,**change)))
                backend=music.Backend(str(self.base/'absent'))
                self.assertFalse(backend.start())
                self.assertIsNone(backend._owned)
        with patch.dict(os.environ,{'KILIX_CONTENT_ROOT':'relative'}):
            with patch.object(music.subprocess,'Popen',side_effect=AssertionError('invalid root spawn')):
                self.assertFalse(music.Backend(str(self.base/'absent')).start())
        with patch.dict(os.environ,{'KILIX95_HOME':'/explicit-desktop'}):
            os.environ.pop('KILIX_CONTENT_ROOT')
            self.assertFalse(music.Backend(str(self.base/'absent')).start())

    def test_query_cancel_and_original_deadline_reap_owned_query(self):
        pid=self.base/'query-pid'
        self.helper.write_text('import os,time\n'+f'open({str(pid)!r},"w").write(str(os.getpid()))\n'+'time.sleep(30)\n')
        for cancellation in (True,False):
            with self.subTest(cancellation=cancellation):
                pid.unlink(missing_ok=True)
                stop=threading.Event();caught=[]
                def run():
                    try:music._host_selection(root=self.root,timeout=2 if cancellation else .15,cancelled=stop)
                    except ValueError as error:caught.append(str(error))
                thread=threading.Thread(target=run);thread.start()
                wait_until(lambda:pid.exists() and pid.read_text())
                child=int(pid.read_text())
                if cancellation:stop.set()
                thread.join(2)
                self.assertFalse(thread.is_alive());self.assertTrue(caught)
                with self.assertRaises(ProcessLookupError):os.kill(child,0)

    def test_cancel_during_spawn_unwinds_the_owned_backend(self):
        backend=music.Backend(str(self.base/'control'))
        selected=music.backend_selection(root=self.root)
        original=subprocess.Popen;created=[]
        def spawn(*args,**kwargs):
            backend.close()
            child=original(*args,**kwargs);created.append(child);return child
        with patch.object(music,'backend_selection',return_value=selected), \
             patch.object(music.subprocess,'Popen',side_effect=spawn):
            self.assertFalse(backend.start())
        self.assertEqual(len(created),1)
        self.assertIsNotNone(created[0].poll())
        self.assertIsNone(backend._owned)

    def test_existing_invalid_endpoint_refuses_without_setup_or_mutation(self):
        endpoint=self.base/'occupied';endpoint.write_text('keep')
        state=music.State();state.backend=music.Backend(str(endpoint))
        with patch.object(music,'_host_selection',side_effect=AssertionError('occupied endpoint setup')):
            state.begin_setup();state._worker.join(2)
        self.assertFalse(state.busy())
        self.assertTrue(state.note)
        self.assertEqual(endpoint.read_text(),'keep')
        self.assertIsNone(state.backend._owned)
        state.close()


class StartupAuthorityTests(unittest.TestCase):
    """Real child/socket/reply cuts; barriers select timing, never fake results."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.executable = backend_fixture(self.base)
        source = self.executable.read_text()
        old = 'reply = {"protocol": 1, "ok": request["protocol"] == 1}'
        self.assertIn(old, source)
        self.executable.write_text(source.replace(old,
            'reply = {"protocol": 2, "ok": True, "max_protocol": 2, '
            '"encodec": False, "live_sources": False}'))
        self.env = patch.dict(os.environ, {"HOME": str(self.base), "PATH": os.defpath,
            "KILIX_AMP": str(self.executable), "KILIX_CONTENT_ROOT": str(self.base / "apps"),
            "PYTHONDONTWRITEBYTECODE": "1"}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.fds = len(os.listdir('/proc/self/fd'))
        self.threads = len(threading.enumerate())

    def tearDown(self):
        self.assertEqual(len(os.listdir('/proc/self/fd')), self.fds)
        self.assertEqual(len(threading.enumerate()), self.threads)

    def helper(self):
        host = self.base / 'host'; (host / 'scripts').mkdir(parents=True)
        launcher = host / 'kilix'; launcher.write_text('#!/bin/sh\nexit 99\n'); launcher.chmod(0o700)
        helper = host / 'scripts/install-kilix-amp.py'
        helper.write_text('import time\ntime.sleep(30)\n')
        return launcher

    def test_resolver_cancel_and_original_deadline_prevent_real_spawn(self):
        launcher = self.helper()
        for ending in ('cancel', 'deadline'):
            with self.subTest(ending=ending):
                stop = threading.Event(); spawned = []; original = subprocess.Popen
                def resolve():
                    if ending == 'cancel': stop.set()
                    else: time.sleep(.12)
                    return str(launcher)
                def spawn(*args, **kwargs):
                    child = original(*args, **kwargs); spawned.append(child); return child
                with patch.object(music, 'kilix_launcher', side_effect=resolve), \
                     patch.object(music.subprocess, 'Popen', side_effect=spawn):
                    with self.assertRaises(ValueError):
                        music._host_selection(cancelled=stop, timeout=.1 if ending == 'deadline' else 5)
                self.assertTrue(all(child.poll() is not None for child in spawned))
                self.assertEqual(spawned, [])

    def test_cancel_racing_with_actual_query_spawn_reaps_before_refusal(self):
        launcher = self.helper(); stop = threading.Event(); spawned = []; original = subprocess.Popen
        def spawn(*args, **kwargs):
            child = original(*args, **kwargs); spawned.append(child); stop.set(); return child
        with patch.object(music, 'kilix_launcher', return_value=str(launcher)), \
             patch.object(music.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(ValueError): music._host_selection(cancelled=stop)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())
        self.assertFalse(Path('/proc', str(spawned[0].pid)).exists())

    def test_owned_and_attached_close_during_real_validation_refuse_success(self):
        for attached in (False, True):
            with self.subTest(attached=attached):
                path = self.base / ('attached' if attached else 'owned')
                external = subprocess.Popen([str(self.executable), '--socket', str(path)]) if attached else None
                if external: wait_until(path.exists)
                inode = path.stat().st_ino if external else None
                backend = music.Backend(str(path)); entered = threading.Event(); release = threading.Event(); answer = []
                original = music_protocol.validate_reply
                def validate(data):
                    result = original(data); entered.set()
                    self.assertTrue(release.wait(5)); return result
                worker = threading.Thread(target=lambda: answer.append(backend.start()))
                try:
                    with patch.object(music_protocol, 'validate_reply', side_effect=validate):
                        worker.start(); self.assertTrue(entered.wait(3)); child = backend._owned
                        backend.close()
                        if child: self.assertIsNotNone(child.poll())
                        release.set(); worker.join(3); self.assertFalse(worker.is_alive())
                    self.assertEqual(answer, [False])
                    self.assertIsNone(backend.identity)
                    self.assertIsNone(backend.version)
                    self.assertEqual(backend.capabilities, {})
                    self.assertIsNone(backend._owned)
                    if external:
                        self.assertIsNone(external.poll()); self.assertEqual(path.stat().st_ino, inode)
                finally:
                    release.set(); backend.close()
                    if worker.is_alive(): worker.join(3)
                    if external: external.kill(); external.wait()

    def test_late_negotiation_refuses_then_same_attached_client_retries(self):
        path = self.base / 'retry'
        external = subprocess.Popen([str(self.executable), '--socket', str(path)])
        backend = music.Backend(str(path)); wait_until(path.exists)
        entered = threading.Event(); release = threading.Event(); answer = []
        original = music_protocol.validate_reply
        def validate(data):
            result = original(data); entered.set(); self.assertTrue(release.wait(3)); return result
        worker = threading.Thread(target=lambda: answer.append(backend.start(timeout=.15)))
        try:
            with patch.object(music_protocol, 'validate_reply', side_effect=validate):
                began = time.monotonic(); worker.start(); self.assertTrue(entered.wait(2))
                time.sleep(max(0, .2 - (time.monotonic() - began)))
                release.set(); worker.join(2)
            self.assertFalse(worker.is_alive()); self.assertEqual(answer, [False])
            self.assertIsNone(backend.identity); self.assertIsNone(backend.version)
            self.assertTrue(backend.start(), backend.error)
            self.assertEqual(backend.identity[-1], external.pid)
            self.assertIsNone(backend._owned)
            backend.close(); self.assertIsNone(external.poll())
        finally:
            release.set(); backend.close()
            if worker.is_alive(): worker.join(3)
            external.kill(); external.wait()

    def test_close_between_actual_negotiation_and_owned_start_handoff(self):
        backend = music.Backend(str(self.base / 'handoff'))
        entered = threading.Event(); release = threading.Event(); answer = []
        negotiate = backend.negotiate
        def hold(**kwargs):
            result = negotiate(**kwargs)
            self.assertTrue(result, backend.error); entered.set()
            self.assertTrue(release.wait(3)); return result
        worker = threading.Thread(target=lambda: answer.append(backend.start()))
        try:
            with patch.object(backend, 'negotiate', side_effect=hold):
                worker.start(); self.assertTrue(entered.wait(2)); child = backend._owned
                self.assertIsNotNone(child); backend.close(); self.assertIsNotNone(child.poll())
                release.set(); worker.join(2)
            self.assertFalse(worker.is_alive()); self.assertEqual(answer, [False])
            self.assertIsNone(backend.identity); self.assertIsNone(backend._owned)
            self.assertFalse(Path('/proc', str(child.pid)).exists())
        finally:
            release.set(); backend.close()
            if worker.is_alive(): worker.join(3)

    def test_close_between_actual_exchange_and_negotiation_publication(self):
        path = self.base / 'publication'
        external = subprocess.Popen([str(self.executable), '--socket', str(path)])
        backend = music.Backend(str(path)); wait_until(path.exists)
        entered = threading.Event(); release = threading.Event(); answer = []
        exchange = backend._exchange
        def hold(*args, **kwargs):
            result = exchange(*args, **kwargs); entered.set()
            self.assertTrue(release.wait(3)); return result
        worker = threading.Thread(target=lambda: answer.append(backend.negotiate()))
        try:
            with patch.object(backend, '_exchange', side_effect=hold):
                worker.start(); self.assertTrue(entered.wait(2)); backend.close()
                release.set(); worker.join(2)
            self.assertFalse(worker.is_alive()); self.assertEqual(answer, [False])
            self.assertIsNone(backend.identity); self.assertIsNone(backend.version)
            self.assertIsNone(external.poll())
        finally:
            release.set(); backend.close()
            if worker.is_alive(): worker.join(3)
            external.kill(); external.wait()


if __name__=='__main__':unittest.main()
