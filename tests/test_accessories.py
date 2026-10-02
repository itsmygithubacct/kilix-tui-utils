"""The promoted accessories reuse the shared shell and keep argv boundaries."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_tui import app, clipboard, openers  # noqa: E402


def load(name):
    path = ROOT / "tools" / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"accessory_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ClipboardTests(unittest.TestCase):
    def test_osc52_payload_is_bounded_and_round_trips(self):
        import base64
        encoded = clipboard.sequence("λ").split(";", 2)[-1][:-1]
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), "λ")
        with self.assertRaises(ValueError):
            clipboard.sequence("x" * (clipboard.MAX_COPY_BYTES + 1))


class OpenerTests(unittest.TestCase):
    def test_editor_is_split_to_argv_without_a_shell(self):
        argv = openers.document_argv(
            "/tmp/a b.txt",
            environ={"EDITOR": "nano --nowrap", "PATH": "/bin"},
        )
        self.assertEqual(argv, ("nano", "--nowrap", "/tmp/a b.txt"))


class CharacterMapTests(unittest.TestCase):
    def test_searches_by_name_and_codepoint(self):
        module = load("character_map")
        rows = module.State("snowman").rows()
        self.assertTrue(any(row.value == "☃" for row in rows))
        rows = module.State("2603").rows()
        self.assertEqual(rows[0].value, "☃")


class FindFilesTests(unittest.TestCase):
    def test_bounded_search_finds_files_and_does_not_follow_symlinks(self):
        module = load("find_files")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "report-final.txt").write_text("ok", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            (outside / "hidden-report.txt").write_text("no", encoding="utf-8")
            (root / "link").symlink_to(outside, target_is_directory=True)
            state = module.State(str(root), "report")
            paths = [Path(row.path).name for row in state.results]
            self.assertIn("report-final.txt", paths)
            # The real directory is visited once; its symlink is never descended.
            self.assertEqual(paths.count("hidden-report.txt"), 1)


class NotepadTests(unittest.TestCase):
    def test_edits_and_atomically_saves_utf8(self):
        module = load("notepad")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_text("hello", encoding="utf-8")
            state = module.State(str(path))
            state.column = len(state.lines[0])
            state.insert(" λ")
            self.assertTrue(state.save())
            self.assertEqual(path.read_text(encoding="utf-8"), "hello λ")
            self.assertFalse(state.dirty)

    def test_failed_load_never_overwrites_original_even_after_editing(self):
        module = load("notepad")
        for payload in (b"\xffinvalid UTF-8", b"x" * (module.MAX_DOCUMENT_BYTES + 1)):
            with self.subTest(size=len(payload)), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "original.txt"
                path.write_bytes(payload)
                state = module.State(str(path))
                self.assertFalse(state.save())
                state.insert("replacement")
                self.assertFalse(state.save())
                self.assertEqual(path.read_bytes(), payload)
                alternate = Path(directory) / "new.txt"
                self.assertTrue(state.save(str(alternate)))
                self.assertEqual(alternate.read_text(), "replacement")
                self.assertFalse(state.save(str(path)))
                self.assertEqual(path.read_bytes(), payload)

    def test_missing_file_can_be_created_and_successful_reload_unblocks_save(self):
        module = load("notepad")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.txt"
            state = module.State(str(path))
            state.insert("new")
            self.assertTrue(state.save())
            path.write_bytes(b"\xff")
            state.load()
            self.assertFalse(state.save())
            path.write_text("restored", encoding="utf-8")
            state.load()
            state.insert("ok")
            self.assertTrue(state.save())
            self.assertEqual(path.read_text(), "resoktored")

    def test_failed_load_recovery_through_keyboard_preserves_original(self):
        module = load("notepad")
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.txt"
            alternate = Path(directory) / "新しい.txt"
            original.write_bytes(b"\xff")
            state = module.State(str(original))
            for char in "hello λ":
                module.handle(char, state)
            module.handle(module.CTRL_S, state)
            self.assertTrue(state.naming)
            module.handle("x", state)
            module.handle(27, state)
            self.assertFalse(state.naming)
            self.assertEqual(state.path, str(original))
            self.assertEqual(state.lines, ["hello λ"])
            self.assertTrue(state.dirty)
            # Selecting the guarded path still refuses to overwrite it and
            # leaves filename entry reachable for another attempt.
            module.handle(module.CTRL_S, state)
            for char in str(original):
                module.handle(char, state)
            module.handle("\n", state)
            self.assertTrue(state.naming)
            self.assertEqual(original.read_bytes(), b"\xff")
            for char in str(alternate):
                module.handle(char, state)
            module.handle("\n", state)
            self.assertFalse(state.naming)
            self.assertFalse(state.dirty)
            self.assertEqual(alternate.read_text(encoding="utf-8"), "hello λ")
            self.assertEqual(original.read_bytes(), b"\xff")

    def test_letters_insert_and_only_arrows_navigate(self):
        module = load("notepad")
        state = module.State()
        for char in "hello hjkl":
            module.handle(ord(char), state)
        self.assertEqual(state.lines, ["hello hjkl"])
        module.handle(app.curses.KEY_LEFT, state)
        self.assertEqual(state.column, 9)
        module.handle(app.curses.KEY_RIGHT, state)
        self.assertEqual(state.column, 10)
        module.handle("\n", state)
        module.handle(app.curses.KEY_UP, state)
        self.assertEqual(state.row, 0)
        module.handle(app.curses.KEY_DOWN, state)
        self.assertEqual(state.row, 1)

    def test_wide_input_loop_delivers_unicode_and_controls(self):
        module = load("notepad")
        state = module.State()
        ticks = []
        # U+0103 equals KEY_UP numerically on ncurses: text must not move.
        events = iter([None, app.curses.KEY_RESIZE, "é", "λ", "ă", "界",
                       app.curses.KEY_LEFT, "!", "\x7f", "\t", "\n",
                       "?", "\x11", "\x11"])

        class Screen:
            def keypad(self, enabled):
                pass

            def timeout(self, milliseconds):
                pass

            def erase(self):
                pass

            def refresh(self):
                pass

            def get_wch(self):
                key = next(events)
                if key is None:
                    raise app.curses.error()
                return key

        with mock.patch.dict(os.environ, {"KILIX_TUI_HEADLESS": "0"}), \
             mock.patch.object(app.curses, "curs_set"), \
             mock.patch.object(app.curses, "wrapper", side_effect=lambda loop: loop(Screen())):
            self.assertEqual(app.run(
                lambda surface, state: None, state, handle=module.handle,
                unicode_input=True, help_key=False, tick_ms=10,
                on_tick=ticks.append), 0)
        self.assertEqual(ticks, [state])
        self.assertEqual(state.lines, ["éλă    ", "?界"])
        self.assertEqual((state.row, state.column), (1, 1))

    def test_notepad_enables_wide_input(self):
        module = load("notepad")
        with mock.patch.object(module.app, "run", return_value=0) as run:
            self.assertEqual(module.main([]), 0)
        self.assertTrue(run.call_args.kwargs["unicode_input"])

    def test_unsaved_quit_requires_the_chord_twice(self):
        module = load("notepad")
        state = module.State()
        state.insert("x")
        self.assertTrue(module.handle(module.CTRL_Q, state))
        self.assertFalse(module.handle(module.CTRL_Q, state))


class RuntimeTests(unittest.TestCase):
    def test_runtime_build_publishes_focused_and_accessory_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "runtime"
            result = subprocess.run(
                ["bash", str(ROOT / "install.sh")],
                env=dict(
                    os.environ,
                    KILIX_TUI_UTILS_PREFIX=str(prefix),
                    KILIX_TUI_UTILS_SYNC_MENU="0",
                ),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = {
                "kilix-system-center", "kilix-settings-center",
                "kilix-software-center", "kilix-session-center",
                "kilix-voice-studio", "kilix-character-map",
                "kilix-find-files", "kilix-notepad", "kilix-panes",
            }
            self.assertTrue(expected <= {path.name for path in (prefix / "bin").iterdir()})
            launcher = (prefix / "bin" / "kilix-system-center").read_text()
            self.assertIn("'--app' 'system'", launcher)


if __name__ == "__main__":
    unittest.main()
