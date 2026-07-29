from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
import unittest
from unittest.mock import patch

from kilix_temps.cli import main


class CliUnitTests(unittest.TestCase):
    @staticmethod
    def _demo_frame(locale_name: str, *arguments: str) -> str:
        output = io.StringIO()
        with patch.dict(os.environ, {"LANG": locale_name}, clear=True):
            with redirect_stdout(output):
                result = main(
                    [
                        "--demo",
                        "--once",
                        "--text",
                        "--no-color",
                        "--width",
                        "100",
                        *arguments,
                    ]
                )
        if result != 0:
            raise AssertionError(f"kilix-temps exited with {result}")
        return output.getvalue()

    def test_locale_selects_human_readable_unit(self) -> None:
        self.assertIn("°F", self._demo_frame("C.UTF-8"))
        self.assertIn("°C", self._demo_frame("en_GB.UTF-8"))

    def test_explicit_unit_overrides_locale(self) -> None:
        self.assertIn(
            "°F",
            self._demo_frame("en_GB.UTF-8", "--fahrenheit"),
        )
        self.assertIn(
            "°C",
            self._demo_frame("en_US.UTF-8", "--celsius"),
        )

    def test_json_contract_remains_celsius(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--demo", "--json", "--fahrenheit"])
        self.assertEqual(result, 0)
        document = json.loads(output.getvalue())
        self.assertIn("celsius", document["temperatures"][0])
        self.assertNotIn("fahrenheit", document["temperatures"][0])


if __name__ == "__main__":
    unittest.main()
