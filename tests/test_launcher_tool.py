"""kilix-launcher: one catalog, argv-only launches, and a run row.

What is pinned: the catalog is assembled from the same sources the TUI
desktop reads (stack registry, shared XDG scan, the desktop-launcher folder),
GUI applications are contained by `kilix run` or dropped rather than spawned
raw, `report`-verb programs keep their hold-until-Enter wrapper, and the run
row never touches a shell.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "launcher"))

import main as launcher  # noqa: E402
from kilix_desk import registry  # noqa: E402
from kilix_tui import app, xdgapps  # noqa: E402

APPS = {
    "Internet": [
        {"id": "firefox.desktop", "name": "Firefox",
         "exec": "firefox --new-window", "terminal": False},
    ],
    "Accessories": [
        {"id": "htop.desktop", "name": "htop", "exec": "htop",
         "terminal": True},
    ],
}


def catalog(kilix=("/opt/kilix/kilix",), launchers=()):
    with mock.patch.object(launcher.xdgapps, "grouped", return_value=APPS), \
         mock.patch.object(launcher.xdgapps, "entries_in",
                           side_effect=[list(launchers), []]), \
         mock.patch.object(launcher.registry, "kilix_command",
                           return_value=list(kilix) if kilix else None):
        return launcher.rows()


class CatalogTests(unittest.TestCase):
    def test_the_run_row_comes_first(self):
        rows = catalog()
        self.assertEqual(rows[0]["kind"], "run")

    def test_stack_programs_come_from_the_shared_registry(self):
        rows = catalog()
        labels = {r["label"] for r in rows if r["kind"] == "program"}
        # Whatever resolves on this machine must be a subset of the same
        # PROGRAMS table the TUI desktop renders — no second list.
        registry_labels = {item.label for item in registry.PROGRAMS}
        self.assertTrue(labels <= registry_labels)

    def test_terminal_apps_run_directly_and_gui_apps_are_contained(self):
        rows = {r["label"]: r for r in catalog()}
        self.assertEqual(rows["htop"]["argv"], ["htop"])
        self.assertEqual(rows["Firefox"]["argv"],
                         ["/opt/kilix/kilix", "run", "firefox",
                          "--new-window"])

    def test_gui_apps_are_dropped_without_a_kilix_to_contain_them(self):
        labels = {r["label"] for r in catalog(kilix=None)}
        self.assertNotIn("Firefox", labels)
        self.assertIn("htop", labels)

    def test_desktop_folder_launchers_join_the_list_once(self):
        entry = {"id": "mine.desktop", "name": "My Thing",
                 "exec": "mything", "terminal": True}
        with mock.patch.object(launcher.xdgapps, "grouped", return_value={}), \
             mock.patch.object(launcher.xdgapps, "entries_in",
                               side_effect=[[entry], [entry]]), \
             mock.patch.object(launcher.registry, "kilix_command",
                               return_value=["/opt/kilix/kilix"]):
            rows = launcher.rows()
        mine = [r for r in rows if r["label"] == "My Thing"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["detail"], "launcher")

    def test_report_verbs_keep_their_hold_until_enter_wrapper(self):
        row = {"kind": "program", "label": "Stack status",
               "argv": ["/opt/kilix/kilix", "status"], "verb": "report"}
        argv = launcher.launch_argv(row)
        self.assertEqual(argv[:2], ["sh", "-c"])
        self.assertIn("press Enter to return", argv[2])


class RunRowTests(unittest.TestCase):
    def make_state(self):
        state = launcher.State.__new__(launcher.State)
        state.all = [{"kind": "run", "label": launcher.RUN_ROW,
                      "detail": ""}]
        state.filter = ""
        state.selected = 0
        state.command = None
        state.message = ""
        return state

    def test_enter_on_the_run_row_opens_the_command_line(self):
        state = self.make_state()
        launcher.handle(10, state)
        self.assertEqual(state.command, "")

    def test_the_command_execs_an_argv_never_a_shell(self):
        state = self.make_state()
        execs = []
        launcher.handle(ord("!"), state)
        for ch in 'printf "two words"':
            launcher.handle(ord(ch), state)
        with mock.patch.object(launcher.os, "execvp",
                               side_effect=lambda p, a: execs.append((p, a))):
            launcher.handle(10, state)
        self.assertEqual(execs, [("printf", ["printf", "two words"])])

    def test_shell_operators_are_refused(self):
        state = self.make_state()
        launcher.handle(ord("!"), state)
        for ch in "ls | wc":
            launcher.handle(ord(ch), state)
        with mock.patch.object(launcher.os, "execvp",
                               side_effect=AssertionError("must not exec")):
            launcher.handle(10, state)
        self.assertIn("pipes", state.message)

    def test_escape_closes_the_command_line(self):
        state = self.make_state()
        launcher.handle(ord("!"), state)
        launcher.handle(27, state)
        self.assertIsNone(state.command)

    def test_a_failed_exec_reports_instead_of_dying(self):
        state = self.make_state()
        launcher.handle(ord("!"), state)
        for ch in "no-such-command-anywhere":
            launcher.handle(ord(ch), state)
        launcher.handle(10, state)          # exec fails, tool stays alive
        self.assertIn("no-such-command-anywhere", state.message)


class SurfaceTests(unittest.TestCase):
    def test_the_screen_renders_headlessly(self):
        with mock.patch.object(launcher, "rows", return_value=[
                {"kind": "run", "label": launcher.RUN_ROW, "detail": ""},
                {"kind": "app", "label": "htop", "detail": "accessories",
                 "argv": ["htop"], "verb": "inplace"}]):
            state = launcher.State()
        text = app.render_to_text(launcher.render, state)
        self.assertIn("Launcher", text)
        self.assertIn("htop", text)

    def test_list_mode_prints_the_catalog(self):
        import io
        from contextlib import redirect_stdout
        with mock.patch.object(launcher, "rows", return_value=[
                {"kind": "app", "label": "htop", "detail": "accessories",
                 "argv": ["htop"], "verb": "inplace"}]):
            out = io.StringIO()
            with redirect_stdout(out):
                code = launcher.main(["--list"])
        self.assertEqual(code, 0)
        self.assertIn("app\thtop\thtop", out.getvalue())


if __name__ == "__main__":
    unittest.main()
