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



class _FakeBackend:
    """A backend that records commands and answers with a status."""

    def __init__(self, status=None, ok=True, error=""):
        self.path = "/run/fake/amp.sock"
        self.error = ""
        self.sent: list[tuple[str, dict]] = []
        self.status = dict(status or {
            "state": "playing", "title": "t", "file": "/m/t.flac",
            "pos": 30.0, "len": 100.0, "index": 1, "count": 3,
            "volume": 50, "shuffle": False, "repeat": 0})
        self._ok = ok
        self._error = error

    def available(self):
        return True

    def installed(self):
        return True

    def command(self, name, **fields):
        self.sent.append((name, dict(fields)))
        if name == "playlist":
            return {"items": ["/m/a.flac", "/m/t.flac", "/m/c.flac"],
                    "index": 1, "count": 3}
        reply = {"ok": self._ok, **self.status}
        if not self._ok:
            reply["error"] = self._error
        return reply

    def last(self, name):
        return [fields for sent, fields in self.sent if sent == name]


def player(**kwargs):
    state = music.State.__new__(music.State)
    state.backend = _FakeBackend(**kwargs)
    state.status = dict(state.backend.status)
    state.playlist = []
    state.section = 0
    state.selected = 0
    state.phase = ""
    state.note = ""
    state.message = ""
    state.help_open = False
    state.prompt = None
    state._worker = None
    state.refresh()
    return state


class TransportTests(unittest.TestCase):
    """Every control kilix-amp exposes should be reachable from the keyboard."""

    def test_seek_moves_relative_to_the_current_position(self):
        state = player()
        music.handle(ord("."), state)                    # +30s from 30s
        self.assertEqual(state.backend.last("seek")[-1]["pos"], 60.0)

    def test_seek_never_runs_past_either_end(self):
        state = player()
        for _ in range(20):
            music.handle(ord(","), state)                # -30s, repeatedly
        self.assertGreaterEqual(state.backend.last("seek")[-1]["pos"], 0.0)
        state = player()
        for _ in range(20):
            music.handle(ord("."), state)
        self.assertLessEqual(state.backend.last("seek")[-1]["pos"], 100.0)

    def test_seek_with_no_track_says_so_instead_of_sending(self):
        state = player(status={"state": "stopped", "pos": 0, "len": 0})
        music.handle(ord("."), state)
        self.assertEqual(state.backend.last("seek"), [])
        self.assertIn("nothing playing", state.message)

    def test_volume_steps_and_clamps_to_the_backend_range(self):
        state = player()
        music.handle(ord("+"), state)
        self.assertEqual(state.backend.last("volume")[-1]["level"], 55)
        state = player(status={"volume": 98, "state": "playing"})
        for _ in range(5):
            music.handle(ord("+"), state)
        self.assertEqual(state.backend.last("volume")[-1]["level"], 100)
        state = player(status={"volume": 2, "state": "playing"})
        for _ in range(5):
            music.handle(ord("-"), state)
        self.assertEqual(state.backend.last("volume")[-1]["level"], 0)

    def test_shuffle_and_repeat_reach_the_backend(self):
        state = player()
        music.handle(ord("s"), state)
        music.handle(ord("m"), state)
        self.assertEqual(len(state.backend.last("shuffle")), 1)
        self.assertEqual(len(state.backend.last("repeat")), 1)

    def test_transport_keys_are_the_ones_the_footer_promises(self):
        state = player()
        for key, command in ((" ", "toggle"), ("b", "next"), ("z", "previous"),
                             ("v", "stop")):
            state.backend.sent.clear()
            music.handle(ord(key), state)
            self.assertTrue(state.backend.last(command), f"{key} -> {command}")


class PlaylistTests(unittest.TestCase):
    def test_enter_plays_the_row_under_the_cursor_not_the_playing_one(self):
        state = player()
        state.section = 1
        state.selected = 2
        music.handle(ord("\n"), state)
        self.assertEqual(state.backend.last("play")[-1]["index"], 2)

    def test_the_cursor_and_the_playing_track_are_marked_differently(self):
        state = player()
        state.section = 1
        state.selected = 0                       # cursor on 0, playing is 1
        text = app.render_to_text(music.render, state, height=20, width=70)
        self.assertIn("▶", text)
        self.assertIn("♪", text)

    def test_adding_a_path_expands_the_home_shorthand(self):
        state = player()
        music.handle(ord("a"), state)
        self.assertTrue(state.typing())
        for letter in "~/Music":
            music.handle(ord(letter), state)
        music.handle(ord("\n"), state)
        self.assertEqual(state.backend.last("add")[-1]["path"],
                         os.path.expanduser("~/Music"))

    def test_a_rejected_path_is_reported_rather_than_silently_dropped(self):
        state = player(ok=False, error="nothing playable at that path")
        state.prompt = "/nope"
        music.handle(ord("\n"), state)
        self.assertIn("nothing playable", state.message)

    def test_typing_a_path_takes_every_character_as_text(self):
        # '?' opens help everywhere else; inside the prompt it is a character,
        # which is why this tool owns the key instead of the shared loop.
        state = player()
        music.handle(ord("a"), state)
        music.handle(ord("?"), state)
        self.assertFalse(state.help_open)
        self.assertEqual(state.prompt, "?")

    def test_escape_abandons_the_prompt_without_adding(self):
        state = player()
        music.handle(ord("a"), state)
        music.handle(ord("x"), state)
        music.handle(27, state)
        self.assertFalse(state.typing())
        self.assertEqual(state.backend.last("add"), [])


class PresentationTests(unittest.TestCase):
    def test_now_playing_shows_what_the_status_already_carries(self):
        state = player(status={"state": "playing", "title": "Kilix Theme",
                               "file": "/m/kilix.flac", "pos": 65.0,
                               "len": 130.0, "index": 0, "count": 3,
                               "volume": 70, "shuffle": True, "repeat": 2})
        text = app.render_to_text(music.render, state, height=20, width=90)
        self.assertIn("Kilix Theme", text)
        self.assertIn("1:05 / 2:10", text)
        self.assertIn("70%", text)
        self.assertIn("shuffle on", text)
        self.assertIn("repeat one", text)
        self.assertIn("track 1 of 3", text)

    def test_the_key_line_keeps_help_and_quit_when_it_cannot_fit(self):
        state = player()
        for width in (100, 80, 60, 40):
            text = app.render_to_text(music.render, state, height=20,
                                      width=width)
            last = text.splitlines()[-1]
            self.assertLessEqual(len(last), width)
            self.assertTrue(last.rstrip().endswith("q quit"), f"{width}")
            self.assertIn("?", last, f"{width}")

if __name__ == "__main__":
    unittest.main()
