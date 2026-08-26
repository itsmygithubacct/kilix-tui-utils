"""`kilix panes new` / `close`: the creation half, and the ways it must refuse.

Creation is the first verb here that makes something rather than reading it, so
the tests that matter are about refusal: it never pads a short list to fit, it
never hands a command string to a shell, and it never claims a prompt landed in
a pane it could not address.
"""
import argparse
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_tui import kitty_rc  # noqa: E402


def load():
    path = ROOT / "tools" / "switcher" / "main.py"
    spec = importlib.util.spec_from_file_location("tool_switcher_create", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SplitList(unittest.TestCase):
    """A per-pane list must match the pane count exactly."""

    def setUp(self):
        self.tool = load()

    def test_absent_list_becomes_blanks(self):
        self.assertEqual(self.tool._split_list("", 3, "pane-dir"), ["", "", ""])

    def test_exact_length_is_kept_and_trimmed(self):
        self.assertEqual(
            self.tool._split_list(" a , b ", 2, "pane-name"), ["a", "b"])

    def test_short_list_is_refused_not_padded(self):
        # Padding would build a surface the caller did not ask for, and would
        # succeed while doing it — the worst available outcome.
        with self.assertRaises(ValueError) as caught:
            self.tool._split_list("a,b", 4, "pane-dir")
        self.assertIn("4 panes", str(caught.exception))

    def test_long_list_is_refused_not_truncated(self):
        with self.assertRaises(ValueError):
            self.tool._split_list("a,b,c", 2, "pane-name")


class CommandIsArgvNeverShell(unittest.TestCase):
    """`--command` is parsed here; a shell never sees it."""

    def setUp(self):
        self.tool = load()

    def test_option_does_not_collide_with_the_subcommand_dest(self):
        # The subparsers own dest="command". An option also called `command`
        # overwrites the subcommand name, and the dispatch then falls through
        # and returns success having done nothing — silent, and exit 0.
        ns = self.tool._cli_parser().parse_args(
            ["new", "--name", "x", "--command", "/bin/sh"])
        self.assertEqual(ns.command, "new")
        self.assertEqual(ns.run, "/bin/sh")

    def test_metacharacters_stay_inert_arguments(self):
        import shlex
        argv = shlex.split("codex --yolo")
        self.assertEqual(argv, ["codex", "--yolo"])
        # A semicolon survives as one argument rather than becoming a separator.
        self.assertEqual(shlex.split("echo 'a; rm -rf b'"), ["echo", "a; rm -rf b"])


class LaunchArgumentShape(unittest.TestCase):
    """The RC arguments each creation call builds."""

    def test_launch_pane_targets_a_tab_by_id(self):
        seen = {}

        def fake_run(args, input_text=None):
            seen["args"] = args
            return "77"

        original = kitty_rc._run
        kitty_rc._run = fake_run
        try:
            pane = kitty_rc.launch_pane(
                ["/bin/sh"], page_id=5, title="worker", cwd="/tmp")
        finally:
            kitty_rc._run = original
        self.assertEqual(pane, 77)
        args = seen["args"]
        # --match selects the TAB for `launch`, which is the opposite of what
        # --match means for most other RC verbs.
        self.assertIn("--match", args)
        self.assertIn("id:5", args)
        self.assertIn("--type=window", args)
        self.assertIn("--", args)
        self.assertEqual(args[args.index("--") + 1:], ["/bin/sh"])

    def test_argv_is_passed_after_a_separator(self):
        seen = {}

        def fake_run(args, input_text=None):
            seen["args"] = args
            return "0"

        original = kitty_rc._run
        kitty_rc._run = fake_run
        try:
            kitty_rc.launch_tab(["codex", "--yolo"], title="office")
        finally:
            kitty_rc._run = original
        args = seen["args"]
        self.assertEqual(args[args.index("--") + 1:], ["codex", "--yolo"])


class ReadinessIsNotAssumed(unittest.TestCase):
    """A created pane is not yet an addressable one."""

    def test_missing_broker_session_times_out_loudly(self):
        original_by_id = kitty_rc.pane_by_id
        kitty_rc.pane_by_id = lambda pane_id: kitty_rc.Pane(id=pane_id)
        try:
            with self.assertRaises(kitty_rc.Unavailable) as caught:
                kitty_rc.await_broker_session(9, timeout=0.05, interval=0.01)
        finally:
            kitty_rc.pane_by_id = original_by_id
        # Naming the pane matters: a bare timeout would resurface later as an
        # unexplained send failure.
        self.assertIn("9", str(caught.exception))

    def test_valid_session_returns_the_pane(self):
        ready = kitty_rc.Pane(id=4, broker_session="0123456789abcdef")
        original = kitty_rc.pane_by_id
        kitty_rc.pane_by_id = lambda pane_id: ready
        try:
            self.assertIs(
                kitty_rc.await_broker_session(4, timeout=1.0, interval=0.01),
                ready,
            )
        finally:
            kitty_rc.pane_by_id = original


class CredentialDelivery(unittest.TestCase):
    """The password reaches the terminal by environment, not by --password-file."""

    def test_run_builds_argv_without_password_file_and_sets_the_variable(self):
        # Deliberately behavioural rather than a source grep: an earlier version
        # of this test searched the file text and failed on the comment that
        # explains why the flag is absent. What matters is the argv and the
        # environment actually handed to the subprocess.
        import subprocess as sp
        seen = {}

        class Done:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_run(command, **kwargs):
            seen["command"] = command
            seen["env"] = kwargs.get("env") or {}
            return Done()

        original_run, original_available = sp.run, kitty_rc.available
        kitty_rc.available = lambda: True
        sp.run = fake_run
        try:
            import os
            os.environ.setdefault("KILIX_RC_PASSWORD", "probe-secret")
            kitty_rc._run(["ls"])
        finally:
            sp.run = original_run
            kitty_rc.available = original_available

        self.assertNotIn("--password-file", seen["command"])
        self.assertEqual(seen["env"].get("KILIX_RC_PASSWORD"), "probe-secret")
        self.assertIn("ls", seen["command"])

    def test_password_is_never_an_argument(self):
        # The value must not appear in argv at all — argv is world-readable
        # through /proc for the lifetime of the call.
        import subprocess as sp
        seen = {}

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(command, **kwargs):
            seen["command"] = list(command)
            return Done()

        original_run, original_available = sp.run, kitty_rc.available
        kitty_rc.available = lambda: True
        sp.run = fake_run
        try:
            import os
            os.environ["KILIX_RC_PASSWORD"] = "probe-secret"
            kitty_rc._run(["ls"])
        finally:
            sp.run = original_run
            kitty_rc.available = original_available
        self.assertNotIn("probe-secret", " ".join(seen["command"]))


if __name__ == "__main__":
    unittest.main()
