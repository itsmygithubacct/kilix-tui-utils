"""Live presentation, asynchronous controls and actual owned backend lifetime."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_music import music, player, handle
from test_music_protocol import LIVE, Peer
from kilix_desk import desk, registry
from kilix_tui import app


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("fixture did not reach the requested state")


def backend_fixture(root):
    executable = root / "backend"
    executable.write_text("#!" + sys.executable + "\n" + '''
import json, os, socket, sys
path = sys.argv[sys.argv.index("--socket") + 1]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
os.chmod(path, 0o600)
identity = os.stat(path).st_ino
server.listen(8)
running = True
while running:
    client, _ = server.accept()
    with client:
        data = bytearray()
        while b"\\n" not in data:
            part = client.recv(8192)
            if not part:
                break
            data.extend(part)
        if not data:
            continue
        request = json.loads(data)
        with open(path + ".requests", "a") as output:
            output.write(json.dumps(request) + "\\n")
        reply = {"protocol": 1, "ok": request["protocol"] == 1}
        if not reply["ok"]:
            reply["error"] = "unsupported protocol"
        client.sendall(json.dumps(reply).encode() + b"\\n")
        if request["cmd"] == "quit" and reply["ok"]:
            running = False
server.close()
if os.path.exists(path) and os.stat(path).st_ino == identity:
    os.unlink(path)
''')
    executable.chmod(0o700)
    return executable


class LivePresentationTests(unittest.TestCase):
    def test_enter_preserves_each_source_entry_during_a_real_state_poll(self):
        for kind in ("add", "file", "encodec-unix"):
            with self.subTest(kind=kind):
                entered, release = threading.Event(), threading.Event()
                def respond(request, client):
                    if request["cmd"] == "state" and not release.is_set():
                        entered.set()
                        release.wait(3)
                    reply = (dict(LIVE) if request["cmd"] == "state" else
                             {"protocol": 2, "ok": True, "max_protocol": 2,
                              "encodec": True, "live_sources": True, "items": []})
                    client.sendall(json.dumps(reply).encode() + b"\n")
                with Peer(respond) as peer:
                    state = music.State()
                    state.backend = music.Backend(peer.path)
                    state.prompt_kind, state.prompt = kind, "/private/selected source"
                    try:
                        state.tick()
                        self.assertTrue(entered.wait(2))
                        music.handle(10, state)
                        self.assertEqual(state.prompt, "/private/selected source")
                        self.assertIn("press Enter", state.message)
                        self.assertEqual([r["cmd"] for r in peer.requests], ["ping", "state"])
                        release.set()
                        state._worker.join(2)
                        self.assertFalse(state.busy())
                        handle(10, state)
                        self.assertIsNone(state.prompt)
                        submitted = [r for r in peer.requests if r["cmd"] in ("add", "open")]
                        self.assertEqual(len(submitted), 1)
                        self.assertEqual(submitted[0]["path"], "/private/selected source")
                        self.assertEqual(submitted[0]["cmd"], "add" if kind == "add" else "open")
                        if kind != "add":
                            self.assertEqual(submitted[0]["source_type"], kind)
                    finally:
                        release.set()
                        state.close()

    def test_live_has_elapsed_and_recovery_without_fabricated_end_or_seek(self):
        state = player(status=LIVE)
        for changes, label in (({}, "playing"), ({"degraded": True}, "recovering"),
                               ({"ended": True, "state": "stopped"}, "ended"),
                               ({"reconnect_required": True}, "reconnect required")):
            state.status = dict(LIVE, **changes)
            state.backend.status = state.status
            with patch.object(state, "tick"):
                text = app.render_to_text(music.render, state, height=24, width=100)
            self.assertIn(f"LIVE · 1:05 elapsed · {label}", text)
            self.assertIn("24 kHz mono", text)
            self.assertIn("6 kb/s", text)
            self.assertIn("2 threads", text)
            self.assertIn("model ready", text)
            self.assertNotIn("1:05 /", text)
            handle(ord("."), state)
            self.assertEqual(state.backend.last("seek"), [])
            self.assertIn("cannot seek", state.message)

    def test_pending_and_missing_model_are_visible(self):
        state = player(status=dict(LIVE, ready=False, model_ready=False, buffering=True,
                                   source_error_code=2, source_error_message="selected model unavailable"))
        with patch.object(state, "tick"):
            text = app.render_to_text(music.render, state, height=24, width=100)
        self.assertIn("model not ready", text)
        self.assertIn("buffering", text)
        self.assertIn("selected model unavailable", text)

    def test_open_keys_send_absolute_sources_and_reconnect_only_on_request(self):
        state = player(status=dict(LIVE, ended=True))
        for key, kind, entry in (("o", "file", "music with spaces.kenc"),
                                 ("u", "encodec-unix", "./stream.sock")):
            handle(ord(key), state)
            state.prompt = entry
            handle(10, state)
            self.assertEqual(state.backend.last("open")[-1],
                             {"source_type": kind, "path": os.path.abspath(entry)})
        state.backend.sent.clear()
        state.tick()
        state._worker.join(2)
        self.assertEqual(state.backend.last("open"), [])
        handle(ord("r"), state)
        self.assertEqual(state.backend.last("open"),
                         [{"source_type": "encodec-unix", "path": LIVE["file"]}])

    def test_terminal_controls_are_removed_and_prompt_labels_match(self):
        state = player(status=dict(LIVE, title="sound\x1b[31m\x07", source_error_message="bad\x9bH"))
        for kind, label in (("file", "file:"), ("encodec-unix", "stream:"), ("add", "add:")):
            state.prompt_kind = kind
            state.prompt = "a\x1b]52;;payload\x07"
            with patch.object(state, "tick"):
                text = app.render_to_text(music.render, state, height=24, width=100)
            self.assertIn(label, text)
            for control in ("\x1b", "\x07", "\x9b"):
                self.assertNotIn(control, text)

    def test_live_footer_and_path_byte_bound(self):
        state = player(status=LIVE)
        self.assertIn("r refresh", music.footer(state))
        state.status = dict(LIVE, ended=True)
        self.assertIn("r reconnect", music.footer(state))
        state.status["source_type"] = "encodec-stdin"
        self.assertIn("r refresh", music.footer(state))
        state.prompt = "é" + "a" * 4092
        music.handle(ord("x"), state)
        self.assertEqual(len(state.prompt.encode("utf-8")), 4095)
        music.handle(ord("x"), state)
        self.assertEqual(len(state.prompt.encode("utf-8")), 4095)
        self.assertIn("too long", state.message)

    def test_live_surface_and_help_survive_resize(self):
        state = player(status=LIVE)
        for height, width in ((30, 120), (24, 80), (14, 60), (8, 25), (3, 8)):
            for help_open in (False, True):
                state.help_open = help_open
                with patch.object(state, "tick"):
                    text = app.render_to_text(music.render, state, height=height, width=width)
                self.assertLessEqual(len(text.splitlines()), height)
                self.assertTrue(all(len(row) <= width for row in text.splitlines()))

    def test_blocked_command_never_blocks_render_or_quit(self):
        state = player(status=LIVE)
        entered = threading.Event()
        release = threading.Event()
        def command(*args, **kwargs):
            entered.set()
            release.wait(3)
            return dict(LIVE)
        with patch.object(state.backend, "command", side_effect=command):
            state.send("stop")
            try:
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                app.render_to_text(music.render, state)
                self.assertFalse(music.handle(ord("q"), state))
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(state.busy())
                state.send("quit")
                self.assertEqual(state.backend.command.call_count, 1)
            finally:
                release.set()
                state._worker.join(2)


class OwnedProcessTests(unittest.TestCase):
    def test_owned_backend_negotiates_and_exits_without_touching_unrelated_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = backend_fixture(root)
            backend = music.Backend(str(root / "control"))
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                with patch.object(music, "backend_selection", return_value={"executable": str(executable), "root": None}):
                    self.assertTrue(backend.start())
                child = backend._owned
                self.assertEqual(backend.identity[-1], child.pid)
                backend.close()
                self.assertIsNotNone(child.poll())
                self.assertIsNone(backend._owned)
                self.assertFalse((root / "control").exists())
                self.assertIsNone(unrelated.poll())
                rows = [json.loads(line) for line in (root / "control.requests").read_text().splitlines()]
                self.assertEqual([r["cmd"] for r in rows], ["ping", "ping", "quit"])
            finally:
                backend.close()
                unrelated.kill(); unrelated.wait()

    def test_attached_backend_is_never_stopped_or_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = backend_fixture(root)
            path = root / "control"
            child = subprocess.Popen([str(executable), "--headless", "--socket", str(path)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_until(path.exists)
                backend = music.Backend(str(path))
                with patch.object(music.subprocess, "Popen", side_effect=AssertionError("attached spawn")):
                    self.assertTrue(backend.start())
                    backend.close()
                self.assertIsNone(backend._owned)
                self.assertIsNone(child.poll())
                rows = [json.loads(line) for line in (root / "control.requests").read_text().splitlines()]
                self.assertEqual([r["cmd"] for r in rows], ["ping", "ping"])
            finally:
                child.kill(); child.wait()

    def test_failed_start_reaps_only_its_child_and_preserves_existing_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "stall"
            executable.write_text("#!" + sys.executable + "\nimport time; time.sleep(30)\n")
            executable.chmod(0o700)
            backend = music.Backend(str(root / "control"))
            spawned = []
            popen = subprocess.Popen
            def capture(*args, **kwargs):
                spawned.append(popen(*args, **kwargs))
                return spawned[-1]
            with patch.object(music, "backend_selection", return_value={"executable": str(executable), "root": None}), \
                 patch.object(music.subprocess, "Popen", side_effect=capture):
                self.assertFalse(backend.start(timeout=.1))
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].poll())
            self.assertIsNone(backend._owned)
            path = root / "control"
            path.write_text("leave this")
            with patch.object(music.subprocess, "Popen", side_effect=AssertionError("unsafe spawn")):
                self.assertFalse(music.Backend(str(path)).start())
            self.assertEqual(path.read_text(), "leave this")

    def test_owned_shutdown_does_not_send_quit_to_replacement_peer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = backend_fixture(root)
            backend = music.Backend(str(root / "control"))
            with patch.object(music, "backend_selection", return_value={"executable": str(executable), "root": None}):
                self.assertTrue(backend.start())
            child = backend._owned
            os.unlink(backend.path)
            try:
                with Peer(path=backend.path) as replacement:
                    backend.close()
                    self.assertIsNotNone(child.poll())
                    self.assertEqual(replacement.requests, [])
                    self.assertTrue(Path(backend.path).exists())
            finally:
                backend.close()

    def test_closing_state_cancels_stalled_attach_without_stopping_peer(self):
        entered = threading.Event()
        release = threading.Event()
        def stall(request, client):
            entered.set(); release.wait(3)
        with Peer(stall) as peer:
            with patch.dict(os.environ, {"KILIX_AMP_SOCKET": peer.path}):
                state = music.State()
            state.tick()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            state.close()
            release.set()
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(state.busy())
            self.assertEqual([r["cmd"] for r in peer.requests], ["ping"])

    def test_cancelled_install_reaps_its_owned_session_leader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "kilix"
            pid_file = root / "pid"
            scripts = root / "scripts"
            scripts.mkdir()
            helper = scripts / "install-kilix-amp.py"
            helper.write_text("import os, time\n"
                                + f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                                + "time.sleep(30)\n")
            launcher.write_text("#!/bin/sh\nexit 1\n")
            launcher.chmod(0o700)
            cancelled = threading.Event()
            result = []
            with patch.object(music, "kilix_launcher", return_value=str(launcher)):
                thread = threading.Thread(target=lambda: result.append(music.install_backend(cancelled=cancelled)))
                thread.start()
                wait_until(lambda: pid_file.exists() and pid_file.read_text())
                pid = int(pid_file.read_text())
                cancelled.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [False])
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            with patch.object(music.subprocess, "Popen", side_effect=AssertionError("invalid timeout spawn")):
                for timeout in (True, 0, -1, float("nan"), float("inf"), 10 ** 400):
                    self.assertFalse(music.install_backend(timeout=timeout))


class RegistryTests(unittest.TestCase):
    def test_music_uses_shared_tab_routing_and_console_fallback(self):
        item = next(item for item in registry.PROGRAMS if item.label == "Music")
        with patch.object(registry.shutil, "which", return_value=None):
            plan = registry.resolve(item)
        self.assertEqual(plan.verb, "tab")
        self.assertEqual(plan.argv[-1], str(Path(music.__file__).resolve()))
        for live in (False, True):
            calls = []
            state = desk.State(runner=lambda argv: calls.append(argv) or 0, live=lambda: live)
            entry = desk.Entry("Music", plan.argv, verb=plan.verb)
            with patch.object(desk.kitty_rc, "launch_tab") as launch:
                desk._launch(state, entry)
            if live:
                launch.assert_called_once_with(list(plan.argv), title="Music")
                self.assertEqual(calls, [])
            else:
                launch.assert_not_called()
                self.assertEqual(calls, [plan.argv])


if __name__ == "__main__":
    unittest.main()
