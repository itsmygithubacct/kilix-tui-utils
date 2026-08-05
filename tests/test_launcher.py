"""kilix-launcher: the rows this catalog adds beyond the shared contract.

The core tool contract — run row first, argv-only launches, `kilix run`
containment, launcher-folder dedupe, shell-free command row — is pinned in
test_launcher_tool.py. These tests pin what rides on top of it: scripts are
only listed when they are executable `*.sh` from the stack's own scripts
directories, and registry submenu entries stay desk places instead of
becoming launch rows.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "launcher"))

import main as launcher  # noqa: E402


class ScriptRowTests(unittest.TestCase):
    def test_only_executable_shell_scripts_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "go.sh"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            (Path(tmp) / "plain.sh").write_text("")     # not executable
            binary = Path(tmp) / "binary"               # executable, not .sh
            binary.write_text("")
            binary.chmod(0o755)
            rows = launcher.script_rows([tmp])
        self.assertEqual([r["label"] for r in rows], ["go.sh"])
        self.assertEqual(rows[0]["argv"], [str(exe)])
        self.assertEqual(rows[0]["kind"], "script")
        self.assertEqual(rows[0]["verb"], "inplace")

    def test_the_script_dirs_are_the_stack_checkouts(self):
        with mock.patch.dict(os.environ, {"KILIX_HOME": "/opt/checkout"}):
            dirs = launcher.script_dirs()
        self.assertEqual(dirs, [os.path.expanduser("~/pleb/scripts"),
                                "/opt/checkout/scripts"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILIX_HOME", None)
            self.assertEqual(launcher.script_dirs(),
                             [os.path.expanduser("~/pleb/scripts")])

    def test_duplicate_names_keep_the_first_directory(self):
        with tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as second:
            for base in (first, second):
                path = Path(base) / "same.sh"
                path.write_text("#!/bin/sh\n")
                path.chmod(0o755)
            rows = launcher.script_rows([first, second])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["argv"][0].startswith(first))


class CatalogShapeTests(unittest.TestCase):
    def test_registry_submenus_stay_desk_places(self):
        submenu = mock.Mock(label="Games", submenu=True)
        program = mock.Mock(label="CPU", submenu=None)
        plan = mock.Mock(argv=("kilix-cpu",), verb="inplace")
        with mock.patch.object(launcher.registry, "PROGRAMS",
                               [submenu, program]), \
                mock.patch.object(launcher.registry, "resolve",
                                  return_value=plan), \
                mock.patch.object(launcher.registry, "kilix_command",
                                  return_value=None), \
                mock.patch.object(launcher.xdgapps, "grouped",
                                  return_value={}), \
                mock.patch.object(launcher.xdgapps, "entries_in",
                                  return_value=[]), \
                mock.patch.object(launcher, "script_rows", return_value=[]):
            labels = {r["label"] for r in launcher.rows()
                      if r["kind"] == "program"}
        self.assertEqual(labels, {"CPU"})


if __name__ == "__main__":
    unittest.main()
