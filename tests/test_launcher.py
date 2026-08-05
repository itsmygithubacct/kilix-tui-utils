"""kilix-launcher: one catalog — stack programs, apps, launchers, scripts.

The catalog exists so no desktop grows a second list UI or a `.desktop`
parser of its own. These tests pin the contract that makes it safe to put
behind every desktop: the run-a-command row never interprets shell syntax,
scripts are only listed when they are executable `*.sh`, graphical
applications are contained in `kilix run`, and a machine without an SDK-1.8
Kilix degrades to a row that says so instead of failing.
"""
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_desk import registry  # noqa: E402
from kilix_tui import app  # noqa: E402


def load():
    path = ROOT / "tools" / "launcher" / "main.py"
    spec = importlib.util.spec_from_file_location("tool_launcher", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeXdg:
    """The five scanner calls the tool makes, without needing a Kilix SDK."""

    def __init__(self, entries=()):
        self.entries = list(entries)

    def scan(self):
        return self.entries

    @staticmethod
    def bucket(_entry):
        return "Accessories"

    @staticmethod
    def parse_desktop_file(path):
        entry, in_group = {}, False
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("[") and s.endswith("]"):
                        in_group = s == "[Desktop Entry]"
                        continue
                    if in_group and "=" in s:
                        k, v = s.split("=", 1)
                        entry.setdefault(k.strip(), v.strip())
        except OSError:
            return None
        return entry or None

    @staticmethod
    def localized(entry, key):
        return entry.get(key)

    @staticmethod
    def strip_field_codes(text):
        for code in ("%F", "%f", "%U", "%u"):
            text = text.replace(code, "")
        return text.strip()

    @staticmethod
    def unescape(text):
        return text

    @staticmethod
    def truthy(value):
        return str(value).strip().lower() == "true"


class StackRowTests(unittest.TestCase):
    def test_the_run_a_command_row_is_pinned_first(self):
        launcher = load()
        with mock.patch.object(registry, "resolve", return_value=None):
            rows = launcher.stack_rows(live=False)
        self.assertTrue(rows[0].command_row)
        self.assertEqual(rows[0].label, launcher.RUN_LABEL)

    def test_registry_submenus_stay_desk_places(self):
        # Games and Screensavers are drill-down places in the desktop; a flat
        # catalog listing their submenu headers would offer rows that cannot
        # launch anything.
        launcher = load()
        rows = launcher.stack_rows(live=True)
        labels = {row.label for row in rows}
        self.assertNotIn("Games", labels)
        self.assertNotIn("Screensavers", labels)

    def test_unresolvable_items_carry_a_reason_not_a_crash(self):
        launcher = load()
        with mock.patch.object(registry, "resolve", return_value=None):
            rows = launcher.stack_rows(live=True)
        for row in rows[1:]:
            self.assertIsNone(row.argv)
            self.assertTrue(row.reason)


class AppRowTests(unittest.TestCase):
    def test_gui_apps_are_contained_in_kilix_run(self):
        launcher = load()
        launcher._XDG = FakeXdg([
            {"name": "Paint", "exec": "bigpaint --new", "terminal": False},
            {"name": "Top", "exec": "htop", "terminal": True},
        ])
        with mock.patch.object(registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            rows = launcher.app_rows()
        by_label = {row.label: row for row in rows}
        self.assertEqual(by_label["Paint"].argv,
                         ("/opt/kilix/kilix", "run", "bigpaint", "--new"))
        self.assertEqual(by_label["Top"].argv, ("htop",))

    def test_no_sdk_means_one_honest_row_not_a_crash(self):
        launcher = load()
        launcher._XDG = False               # what a pre-1.8 host resolves to
        rows = launcher.app_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].argv)
        self.assertIn("SDK 1.8", rows[0].reason)


class LauncherRowTests(unittest.TestCase):
    def test_desktop_folder_launchers_are_read_with_the_shared_parser(self):
        launcher = load()
        launcher._XDG = FakeXdg()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "notes.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=My Notes\n"
                "Exec=notepad %F\nX-Kilix-Open=tab\n")
            Path(tmp, "junk.txt").write_text("not a launcher")
            rows = launcher.launcher_rows(tmp)
        self.assertEqual([row.label for row in rows], ["My Notes"])
        self.assertEqual(rows[0].argv, ("notepad",))

    def test_an_empty_desktop_folder_says_so(self):
        launcher = load()
        launcher._XDG = FakeXdg()
        with tempfile.TemporaryDirectory() as tmp:
            rows = launcher.launcher_rows(tmp)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].argv)


class ScriptRowTests(unittest.TestCase):
    def test_only_executable_shell_scripts_are_listed(self):
        launcher = load()
        with tempfile.TemporaryDirectory() as tmp:
            runnable = Path(tmp, "backup.sh")
            runnable.write_text("#!/bin/sh\n")
            runnable.chmod(runnable.stat().st_mode | stat.S_IXUSR)
            Path(tmp, "notes.sh").write_text("not executable")
            plain = Path(tmp, "helper.py")
            plain.write_text("#!/usr/bin/env python3\n")
            plain.chmod(plain.stat().st_mode | stat.S_IXUSR)
            rows = launcher.script_rows([tmp, os.path.join(tmp, "absent")])
        self.assertEqual([row.label for row in rows], ["backup.sh"])
        self.assertEqual(rows[0].argv, (str(runnable),))

    def test_the_script_dirs_are_the_stack_checkouts(self):
        # The same presence-gated pair the reference desktop's System menu
        # walks: the pleb session manager's scripts and Kilix's own.
        launcher = load()
        dirs = launcher.script_dirs()
        self.assertEqual(len(dirs), 2)
        self.assertTrue(dirs[0].endswith(os.path.join("pleb", "scripts")))
        self.assertTrue(dirs[1].endswith("scripts"))


class CommandRowTests(unittest.TestCase):
    def make_state(self, launcher):
        calls = []
        state = launcher.State(runner=lambda argv: calls.append(tuple(argv)) or 0,
                               live=lambda: False)
        state.rows_by_section = {0: launcher.stack_rows(live=False)}
        return state, calls

    def test_the_command_row_runs_what_was_typed_shell_free(self):
        launcher = load()
        state, calls = self.make_state(launcher)
        launcher.handle(ord("!"), state)
        self.assertEqual(state.mode, "command")
        for ch in 'echo "a b" ; rm x':
            launcher.handle(ord(ch), state)
        launcher.handle(ord("\n"), state)
        # shlex splits; the `;` is an argument, never a second command.
        self.assertEqual(calls, [("echo", "a b", ";", "rm", "x")])
        self.assertEqual(state.mode, "browse")

    def test_escape_cancels_without_running_anything(self):
        launcher = load()
        state, calls = self.make_state(launcher)
        launcher.handle(ord("!"), state)
        for ch in "poweroff":
            launcher.handle(ord(ch), state)
        launcher.handle(27, state)
        self.assertEqual(calls, [])
        self.assertEqual(state.mode, "browse")

    def test_enter_on_the_pinned_row_opens_the_prompt(self):
        launcher = load()
        state, _calls = self.make_state(launcher)
        state.cursor = 0
        launcher.handle(ord("\n"), state)
        self.assertEqual(state.mode, "command")

    def test_a_disabled_row_reports_its_reason(self):
        launcher = load()
        state, calls = self.make_state(launcher)
        state.rows_by_section = {0: [launcher.Row("x", reason="not installed")]}
        launcher.handle(ord("\n"), state)
        self.assertEqual(calls, [])
        self.assertEqual(state.message, "not installed")


class RenderTests(unittest.TestCase):
    def test_sections_and_rows_render_headlessly(self):
        launcher = load()
        state = launcher.State(live=lambda: False)
        state.rows_by_section = {
            0: [launcher.Row(launcher.RUN_LABEL, command_row=True),
                launcher.Row("Music", argv=("kilix-music",), right="music")],
        }
        frame = app.render_to_text(launcher.render, state,
                                   height=24, width=100)
        self.assertIn("Launcher", frame)
        for section in launcher.SECTIONS:
            self.assertIn(section, frame)
        self.assertIn(launcher.RUN_LABEL, frame)
        self.assertIn("Music", frame)


if __name__ == "__main__":
    unittest.main()
