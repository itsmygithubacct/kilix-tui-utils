"""kilix-music drives kilix-amp; the contract between them is what breaks.

The socket path rule and the protocol version are shared with a second
repository, so they are asserted literally here rather than read back out of
the client. The rest is about restraint: this tool is installed
unconditionally, so importing it, building its state and drawing it must never
start a process or touch the network.
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_tui import app  # noqa: E402


def load():
    path = ROOT / "tools" / "music" / "main.py"
    spec = importlib.util.spec_from_file_location("tool_music", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


music = load()


class ContractTests(unittest.TestCase):
    """Anything asserted here also has to hold in kilix-amp."""

    def test_protocol_version_is_one(self):
        self.assertEqual(music.PROTOCOL_VERSION, 1)

    def test_socket_path_prefers_the_explicit_override(self):
        with _environment(KILIX_AMP_SOCKET="/custom/amp.sock",
                          XDG_RUNTIME_DIR="/run/user/1000"):
            self.assertEqual(music.socket_path(), "/custom/amp.sock")

    def test_socket_path_falls_back_through_the_runtime_dir(self):
        with _environment(KILIX_AMP_SOCKET=None,
                          XDG_RUNTIME_DIR="/run/user/1000"):
            self.assertEqual(music.socket_path(),
                             "/run/user/1000/kilix-amp.sock")

    def test_socket_path_falls_back_to_the_kilix_session_directory(self):
        with _environment(KILIX_AMP_SOCKET=None, XDG_RUNTIME_DIR=None,
                          HOME="/home/tester"):
            self.assertEqual(
                music.socket_path(),
                "/home/tester/.local/gpu_terminal/kilix/session/"
                "kilix-amp.sock")

    def test_an_empty_runtime_dir_is_not_a_runtime_dir(self):
        """`os.environ.get(...) or default` treats "" as unset; so must this."""
        with _environment(KILIX_AMP_SOCKET=None, XDG_RUNTIME_DIR="",
                          HOME="/home/tester"):
            self.assertTrue(music.socket_path().startswith("/home/tester/"))

    def test_a_reply_from_another_protocol_is_reported_not_used(self):
        with _server({"protocol": 99, "ok": True, "state": "playing"}) as path:
            backend = music.Backend(path)
            self.assertEqual(backend.command("state"), {})
            self.assertIn("99", backend.error)
            self.assertEqual(backend.version, 99)

    def test_a_matching_reply_is_returned(self):
        reply = {"protocol": 1, "ok": True, "state": "playing",
                 "title": "Ode to Joy", "pos": 1.5, "len": 17.7}
        with _server(reply) as path:
            backend = music.Backend(path)
            self.assertEqual(backend.command("state"), reply)
            self.assertEqual(backend.error, "")

    def test_a_request_carries_the_command_and_the_version(self):
        seen = []
        with _server({"protocol": 1, "ok": True}, seen) as path:
            music.Backend(path).command("play", index=3)
        self.assertEqual(seen[0], {"cmd": "play", "protocol": 1, "index": 3})

    def test_a_malformed_reply_is_reported_not_raised(self):
        with _server("not json", raw=True) as path:
            backend = music.Backend(path)
            self.assertEqual(backend.command("state"), {})
            self.assertIn("malformed", backend.error)


class ClockTests(unittest.TestCase):
    """A track position is not an uptime; minutes alone say nothing here."""

    def test_seconds_are_visible_below_a_minute(self):
        self.assertEqual(music.clock(0), "0:00")
        self.assertEqual(music.clock(7), "0:07")
        self.assertEqual(music.clock(17.777), "0:17")

    def test_minutes_and_hours(self):
        self.assertEqual(music.clock(65), "1:05")
        self.assertEqual(music.clock(600), "10:00")
        self.assertEqual(music.clock(3661), "1:01:01")

    def test_nonsense_is_zero_rather_than_an_exception(self):
        self.assertEqual(music.clock(-5), "0:00")
        self.assertEqual(music.clock(None), "0:00")
        self.assertEqual(music.clock("what"), "0:00")


class RestraintTests(unittest.TestCase):
    """A tool one keystroke from a menu must be inert until asked."""

    def test_no_backend_means_no_error_only_a_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _environment(KILIX_AMP_SOCKET=os.path.join(tmp, "absent")):
                state = music.State()
                self.assertEqual(state.playlist, [])
                frame = app.render_to_text(music.render, state,
                                           height=24, width=100)
                self.assertIn("kilix-amp backend not running", frame)

    def test_building_state_and_rendering_start_no_process(self):
        started = []
        original = subprocess.Popen
        subprocess.Popen = lambda *a, **k: started.append(a) # noqa: E731
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with _environment(
                        KILIX_AMP_SOCKET=os.path.join(tmp, "absent")):
                    app.render_to_text(music.render, music.State())
        finally:
            subprocess.Popen = original
        self.assertEqual(started, [])

    def test_it_spawns_only_the_backend_and_the_kilix_installer(self):
        """Read out of the source: two spawn sites, and both are known.

        A front end that launched the player windowed, or that cloned kilix-amp
        itself instead of asking Kilix to, would stop being a client of one
        pinned backend.
        """
        import ast
        tree = ast.parse((ROOT / "tools" / "music" / "main.py").read_text())
        # Qualified by module: this tool's own event loop is `app.run`, which
        # is not a spawn and must not be counted as one.
        spawns = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id, node.func.attr) in (
                ("subprocess", "Popen"), ("subprocess", "run"),
                ("subprocess", "call"), ("subprocess", "check_output"),
                ("subprocess", "check_call"), ("os", "system"),
                ("os", "popen"), ("os", "execv"), ("os", "spawnv"),
            )
        ]
        self.assertEqual(len(spawns), 2, "exactly two spawn sites")
        literals = [
            [element.value for element in node.elts
             if isinstance(element, ast.Constant)]
            for node in ast.walk(tree) if isinstance(node, ast.List)
        ]
        self.assertIn(["--headless", "--socket"], literals)
        self.assertIn(["amp", "--install-only"], literals)
        self.assertNotIn("git", [word for row in literals for word in row])

    def test_setup_runs_off_the_ui_thread(self):
        """A first install compiles kilix-amp; the loop must keep redrawing."""
        entered = threading.Event()
        release = threading.Event()

        def fake_install(*args, **kwargs):
            entered.set()
            release.wait(5)
            return False

        with tempfile.TemporaryDirectory() as tmp:
            with _environment(KILIX_AMP_SOCKET=os.path.join(tmp, "absent"),
                              KILIX_AMP=os.path.join(tmp, "nothing-here"),
                              PATH=tmp):
                state = music.State()
                original = music.install_backend
                music.install_backend = fake_install
                try:
                    state.begin_setup()
                    # Wait for the worker rather than assuming it has been
                    # scheduled: the point of the test is that begin_setup
                    # returned without waiting for the install itself.
                    self.assertTrue(entered.wait(5), "install never started")
                    self.assertTrue(state.busy())
                    self.assertEqual(state.phase, "installing")
                    frame = app.render_to_text(music.render, state,
                                               height=24, width=100)
                    self.assertIn("building the Media Player", frame)
                finally:
                    release.set()
                    for _ in range(100):
                        if not state.busy():
                            break
                        time.sleep(0.05)
                    music.install_backend = original
        self.assertFalse(state.busy())
        self.assertEqual(state.phase, "")
        self.assertIn("could not install", state.note)

    def test_install_asks_kilix_rather_than_cloning(self):
        seen = []

        class _Result:
            returncode = 0

        original = subprocess.run
        subprocess.run = lambda argv, **k: (seen.append(argv), _Result())[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                launcher = os.path.join(tmp, "kilix")
                with open(launcher, "w") as handle:
                    handle.write("#!/bin/sh\n")
                os.chmod(launcher, 0o755)
                with _environment(PATH=tmp, KILIX_AMP=""):
                    music.install_backend()
        finally:
            subprocess.run = original
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1:], ["amp", "--install-only"])

    def test_install_without_a_kilix_command_fails_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _environment(PATH=tmp,
                              GPU_TERMINAL_SOURCE_HOME=tmp,
                              KILIX_AMP=os.path.join(tmp, "nothing-here")):
                if music.kilix_launcher():
                    self.skipTest("a kilix checkout is reachable from here")
                self.assertFalse(music.install_backend())

    def test_start_reports_a_missing_binary_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _environment(KILIX_AMP_SOCKET=os.path.join(tmp, "absent"),
                              KILIX_AMP=os.path.join(tmp, "nothing-here"),
                              PATH=tmp):
                backend = music.Backend()
                self.assertFalse(backend.start(timeout=0.1))
                self.assertIn("not built", backend.error)


class _environment:
    """Set or unset environment variables for the duration of a block."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class _server:
    """A one-shot stand-in for kilix-amp's control socket."""

    def __init__(self, reply, seen=None, raw=False):
        self.reply = reply
        self.seen = seen
        self.raw = raw

    def __enter__(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "amp.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self.path

    def _serve(self):
        try:
            client, _ = self.sock.accept()
        except OSError:
            return
        with client:
            data = client.recv(65536).decode("utf-8", errors="replace")
            if self.seen is not None and data.strip():
                self.seen.append(json.loads(data.splitlines()[0]))
            body = (self.reply if self.raw
                    else json.dumps(self.reply)) + "\n"
            try:
                client.sendall(body.encode("utf-8"))
            except OSError:
                pass

    def __exit__(self, *exc):
        self.sock.close()
        self.thread.join(timeout=2)
        for name in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)
        return False


if __name__ == "__main__":
    unittest.main()
