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

    def test_the_only_command_it_spawns_is_kilix_amp(self):
        """Read out of the source: the front end runs one program, headless.

        A front end that grew a second spawn site, or one that launched the
        player windowed, would stop being a client of the shared backend.
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
        self.assertEqual(len(spawns), 1, "exactly one spawn site")
        headless = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.List)
            and any(isinstance(element, ast.Constant)
                    and element.value == "--headless"
                    for element in node.elts)
        ]
        self.assertEqual(len(headless), 1,
                         "one command literal, and it is the headless one")

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
