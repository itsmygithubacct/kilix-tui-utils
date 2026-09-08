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


if __name__=='__main__':unittest.main()
