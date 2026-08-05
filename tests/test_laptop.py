"""kilix-launcher's laptop rows: the host verb's registry, in the catalog.

The laptop session profiles live host-side (`kilix laptop`, which owns the
shared run registry beside ~/.local/gpu_terminal/laptop). The rows are
deliberately thin: the verb is probed before it is believed — an old
launcher forwards unknown words to the terminal engine, so exit codes alone
lie — `kilix laptop status` renders verbatim, and Enter maps to `open` for
a stopped profile and `close` for a running one, run in place with the
catalog refreshed from the registry's new answer. These tests pin that
thinness.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "launcher"))

import main as launcher  # noqa: E402

KILIX = ["/opt/kilix/kilix"]
USAGE = "usage: kilix laptop [list|open PROFILE|status|close PROFILE]"


def completed(stdout="", rc=0, stderr=""):
    return mock.Mock(returncode=rc, stdout=stdout, stderr=stderr)


class LaptopRowTests(unittest.TestCase):
    def setUp(self):
        launcher._LAPTOP_VERB = None

    def tearDown(self):
        launcher._LAPTOP_VERB = None

    def test_probe_requires_the_usage_token(self):
        # Exit 0 without the token is an old launcher echoing something else.
        with mock.patch.object(launcher.subprocess, "run",
                               return_value=completed(stdout="who?")):
            self.assertFalse(launcher._laptop_verb_available(KILIX))
        launcher._LAPTOP_VERB = None
        with mock.patch.object(launcher.subprocess, "run",
                               return_value=completed(stdout=USAGE)):
            self.assertTrue(launcher._laptop_verb_available(KILIX))

    def test_no_verb_degrades_to_a_reason_row(self):
        rows = launcher.laptop_rows(None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "note")
        self.assertIn("kilix laptop", rows[0]["detail"])
        self.assertNotIn("action", rows[0])

    def test_status_report_becomes_rows(self):
        answers = [completed(stdout=USAGE),
                   completed(stdout="coding running (pid 4242)\n"
                                    "remote-ops stopped\n"
                                    "cap-desk desktop\n")]
        with mock.patch.object(launcher.subprocess, "run",
                               side_effect=answers):
            rows = launcher.laptop_rows(KILIX)
        by = {r["label"]: r for r in rows}
        self.assertEqual(by["coding"]["action"],
                         [*KILIX, "laptop", "close", "coding"])
        self.assertIn("Enter closes", by["coding"]["detail"])
        self.assertIn("running (pid 4242)", by["coding"]["detail"])
        self.assertEqual(by["remote-ops"]["action"],
                         [*KILIX, "laptop", "open", "remote-ops"])
        self.assertEqual(by["cap-desk"]["action"],
                         [*KILIX, "laptop", "open", "cap-desk"])

    def test_enter_runs_the_action_in_place_and_refreshes(self):
        row = {"kind": "laptop", "label": "coding",
               "detail": "running (pid 4242) · Enter closes",
               "action": [*KILIX, "laptop", "close", "coding"]}
        state = launcher.State.__new__(launcher.State)
        state.all = [row]
        state.filter = ""
        state.selected = 0
        state.command = None
        state.message = ""
        calls = []

        def record(argv, **_kwargs):
            calls.append(list(argv))
            return completed()

        with mock.patch.object(launcher.subprocess, "run",
                               side_effect=record), \
                mock.patch.object(launcher, "rows",
                                  return_value=[]) as refreshed, \
                mock.patch.object(launcher.os, "execvp",
                                  side_effect=AssertionError("must not exec")):
            self.assertTrue(launcher.activate(state))
        self.assertEqual(calls, [[*KILIX, "laptop", "close", "coding"]])
        refreshed.assert_called_once()
        self.assertIn("coding", state.message)

    def test_an_empty_registry_reads_as_no_profiles(self):
        answers = [completed(stdout=USAGE), completed(stdout="")]
        with mock.patch.object(launcher.subprocess, "run",
                               side_effect=answers):
            rows = launcher.laptop_rows(KILIX)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "note")


if __name__ == "__main__":
    unittest.main()
