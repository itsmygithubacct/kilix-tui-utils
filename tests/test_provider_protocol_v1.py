#!/usr/bin/env python3
"""Protocol-v1 tests for the composed Kilix TUI desktop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "kilix-tui" / "main.py"


class ProviderProtocolV1Tests(unittest.TestCase):
    def run_provider(self, *arguments: str):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, str(ENTRY), *arguments],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def json_endpoint(self, *arguments: str) -> dict[str, object]:
        result = self.run_provider(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertFalse(result.stdout.endswith("\n\n"))
        return json.loads(result.stdout)

    def test_describe_and_check(self):
        description = self.json_endpoint("provider", "describe", "--json")
        self.assertEqual(description["provider_id"], "kilix-tui")
        self.assertEqual(description["contract_version"], 1)
        self.assertEqual(
            description["display_modes"], ["terminal-text", "kitty-graphics"]
        )
        self.assertTrue(description["capabilities"]["headless_screenshot"])
        check = self.json_endpoint("provider", "check", "--json")
        self.assertEqual(check["status"], "ready")
        self.assertTrue(all(item["status"] == "pass" for item in check["checks"]))

    def test_config_read_endpoints_and_gated_mutations(self):
        schema = self.json_endpoint("provider", "config", "schema", "--json")
        self.assertEqual(schema["x-kilix-provider-id"], "kilix-tui")
        values = self.json_endpoint("provider", "config", "get", "--json")
        self.assertEqual(values["values"], {})
        for arguments in (
            ("provider", "config", "set", "theme", "tango"),
            ("provider", "migrate", "--from", "0.3.1", "--dry-run"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_provider(*arguments)
                self.assertEqual(result.returncode, 4)
                self.assertEqual(result.stdout, "")
                self.assertIn("unavailable", result.stderr)

    def test_provider_screenshot_reuses_headless_renderer_without_stdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "desktop.txt"
            result = self.run_provider("provider", "screenshot", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue(output.read_text(encoding="utf-8").endswith("\n"))

    def test_launch_translation_preserves_session_identity(self):
        module_path = ROOT / "kilix-tui"
        sys.path.insert(0, str(module_path))
        try:
            import provider_protocol

            translated = provider_protocol.dispatch(
                ["provider", "launch", "--session-id", "tui-session.1"]
            )
            self.assertEqual(translated, [])
            self.assertEqual(
                os.environ["KILIX_DESKTOP_SESSION_ID"], "tui-session.1"
            )
        finally:
            sys.path.remove(str(module_path))
            sys.modules.pop("provider_protocol", None)
            os.environ.pop("KILIX_DESKTOP_SESSION_ID", None)

    def test_invalid_session_id_fails_closed(self):
        result = self.run_provider(
            "provider", "launch", "--session-id", "bad/session"
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
