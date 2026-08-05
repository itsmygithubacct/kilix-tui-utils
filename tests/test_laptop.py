"""kilix-launcher's Laptop section: the host verb's registry, in a list.

The laptop session profiles live host-side (`kilix laptop`, which owns
the shared run registry beside ~/.local/gpu_terminal/laptop). This
section is deliberately thin: it probes the verb before believing it
exists — an old launcher forwards unknown words to the terminal engine,
so exit codes alone lie — renders exactly what `kilix laptop status`
reports, and maps Enter to `open` for a stopped profile and `close` for
a running one, run in place with the section refreshed from the
registry's new answer. These tests pin that thinness: no probe, no
rows; a fake verb's report renders verbatim; the actions are the exact
argv the host documents.
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

from kilix_tui import app  # noqa: E402


def load():
    path = ROOT / "tools" / "launcher" / "main.py"
    spec = importlib.util.spec_from_file_location("tool_launcher_laptop",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FAKE_KILIX = """#!/bin/sh
[ "$1" = laptop ] || exit 2
case "$2" in
  help)
    echo "usage: kilix laptop [list|open PROFILE|status|close PROFILE]"
    exit 0 ;;
  status)
    echo "bench running (pid 4242)"
    echo "house desktop"
    echo "notes stopped"
    echo "broken invalid"
    exit 0 ;;
  open|close)
    echo "$2 $3" >> "$KILIX_LAPTOP_TEST_LOG"
    echo "laptop $3: ${2}ed"
    exit 0 ;;
esac
exit 2
"""


class LaptopSectionTests(unittest.TestCase):
    def setUp(self):
        self.launcher = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kilix = Path(self.tmp.name) / "kilix"
        self.kilix.write_text(FAKE_KILIX)
        self.kilix.chmod(self.kilix.stat().st_mode | stat.S_IEXEC)
        self.log = Path(self.tmp.name) / "actions.log"
        os.environ["KILIX_LAPTOP_TEST_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "KILIX_LAPTOP_TEST_LOG", None)

    def _with_fake_kilix(self):
        self.launcher._LAPTOP_VERB = None
        return mock.patch.object(self.launcher, "_kilix",
                                 lambda: [str(self.kilix)])

    def test_laptop_is_a_catalog_section(self):
        self.assertIn("Laptop", self.launcher.SECTIONS)
        # Appended, so the four original sections keep their numbers.
        self.assertEqual(self.launcher.SECTIONS.index("Laptop"), 4)

    def test_no_verb_degrades_to_a_reason_row(self):
        # A host that predates `kilix laptop` gets a row that says so —
        # the SDK-1.8 degradation idiom, not a failure.
        self.launcher._LAPTOP_VERB = None
        with mock.patch.object(self.launcher, "_kilix", lambda: None):
            rows = self.launcher.laptop_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].argv)
        self.assertIn("kilix laptop", rows[0].reason)

    def test_probe_requires_the_usage_token(self):
        # Exit 0 without the token must read as "no verb": an old kilix
        # hands unknown words to the engine, which may exit 0 too.
        chatty = Path(self.tmp.name) / "chatty"
        chatty.write_text("#!/bin/sh\necho something else\nexit 0\n")
        chatty.chmod(chatty.stat().st_mode | stat.S_IEXEC)
        self.launcher._LAPTOP_VERB = None
        with mock.patch.object(self.launcher, "_kilix",
                               lambda: [str(chatty)]):
            self.assertFalse(self.launcher._laptop_verb_available())

    def test_status_report_becomes_rows(self):
        with self._with_fake_kilix():
            rows = self.launcher.laptop_rows()
        by_label = {row.label: row for row in rows}
        self.assertEqual(set(by_label),
                         {"bench", "house", "notes", "broken"})
        running = by_label["bench"]
        self.assertIn("running (pid 4242)", running.right)
        self.assertIn("Enter closes", running.right)
        self.assertEqual(running.argv[-3:], ("laptop", "close", "bench"))
        self.assertTrue(running.sync)
        stopped = by_label["notes"]
        self.assertEqual(stopped.right, "stopped")
        self.assertEqual(stopped.argv[-3:], ("laptop", "open", "notes"))
        self.assertTrue(stopped.sync)
        self.assertEqual(by_label["house"].right, "desktop")
        self.assertEqual(by_label["house"].argv[-3:],
                         ("laptop", "open", "house"))
        self.assertIsNone(by_label["broken"].argv)

    def test_enter_runs_the_action_in_place_and_refreshes(self):
        with self._with_fake_kilix():
            state = self.launcher.State(live=lambda: False)
            state.section = self.launcher.SECTIONS.index("Laptop")
            state.rows_by_section = {
                state.section: self.launcher.laptop_rows()}
            rows = state.rows()
            state.cursor = next(i for i, row in enumerate(rows)
                                if row.label == "bench")
            self.launcher._open(state)
        self.assertEqual(self.log.read_text().strip(), "close bench")
        self.assertEqual(state.message, "laptop bench: closeed")
        # The section was re-read from the registry, not left stale.
        self.assertTrue(state.rows_by_section[state.section])

    def test_open_action_reaches_the_verb(self):
        with self._with_fake_kilix():
            state = self.launcher.State(live=lambda: False)
            state.section = self.launcher.SECTIONS.index("Laptop")
            state.rows_by_section = {
                state.section: self.launcher.laptop_rows()}
            rows = state.rows()
            state.cursor = next(i for i, row in enumerate(rows)
                                if row.label == "notes")
            self.launcher._open(state)
        self.assertEqual(self.log.read_text().strip(), "open notes")

    def test_laptop_section_renders_headlessly(self):
        with self._with_fake_kilix():
            state = self.launcher.State(live=lambda: False)
            state.section = self.launcher.SECTIONS.index("Laptop")
            state.rows_by_section = {
                state.section: self.launcher.laptop_rows()}
            frame = app.render_to_text(self.launcher.render, state,
                                       height=24, width=100)
        self.assertIn("Laptop", frame)
        self.assertIn("bench", frame)
        self.assertIn("Enter closes", frame)


if __name__ == "__main__":
    unittest.main()
