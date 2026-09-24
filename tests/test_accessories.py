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
