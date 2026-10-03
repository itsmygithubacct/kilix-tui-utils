"""The text desktop hands questions to the RC5 help lookup as argv."""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_desk import registry


def load_tool():
    path = ROOT / "tools/help_search/main.py"
    spec = importlib.util.spec_from_file_location("help_search_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HelpSearchTests(unittest.TestCase):
    def test_programs_menu_opens_the_bundled_search_prompt(self):
        item = next(item for item in registry.PROGRAMS if item.label == "Help Search")
        with mock.patch.object(registry.shutil, "which", return_value=None):
            plan = registry.resolve(item)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.argv, (sys.executable,
                                     str(ROOT / "tools/help_search/main.py")))

    def test_interactive_question_calls_existing_lookup_without_a_shell(self):
        tool = load_tool()
        prompts = []
        calls = []

        def ask(prompt):
            prompts.append(prompt)
            return "How do I split a pane?" if len(prompts) == 1 else ""

        def run(argv, **options):
            calls.append((argv, options))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(tool.registry, "kilix_command", return_value=["/opt/kilix/kilix"]):
            self.assertEqual(tool.main([], ask=ask, run=run), 0)
        self.assertEqual(calls, [(["/opt/kilix/kilix", "help-search", "-k", "5",
                                   "--full", "--", "How do I split a pane?"],
                                  {"check": False})])
        self.assertEqual(len(prompts), 2)

    def test_direct_question_and_missing_launcher(self):
        tool = load_tool()
        errors = io.StringIO()
        with mock.patch.object(tool.registry, "kilix_command", return_value=None):
            self.assertEqual(tool.main(["why", "does", "--iso", "work?"], errors=errors), 69)
        self.assertIn("unavailable", errors.getvalue())

    def test_runtime_installs_help_search_command(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "runtime"
            env = dict(os.environ, KILIX_TUI_UTILS_PREFIX=str(prefix),
                       KILIX_TUI_UTILS_SYNC_MENU="0")
            result = subprocess.run(["bash", str(ROOT / "install.sh")],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = prefix / "bin/kilix-help-search"
            self.assertTrue(launcher.is_file())
            self.assertTrue(os.access(launcher, os.X_OK))


if __name__ == "__main__":
    unittest.main()
