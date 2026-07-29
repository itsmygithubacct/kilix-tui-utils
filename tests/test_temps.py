"""Run the Kilix Temps unit suite from the unified repository test runner."""
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = ROOT / "tools" / "temps"
sys.path.insert(0, str(TOOL_ROOT))


def load_tests(
    loader: unittest.TestLoader,
    tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    del pattern
    migrated = loader.discover(
        str(TOOL_ROOT / "tests"),
        pattern="test_*.py",
        top_level_dir=str(TOOL_ROOT / "tests"),
    )
    return unittest.TestSuite((tests, migrated))


class RepositoryIntegrationTests(unittest.TestCase):
    def test_temps_uses_the_repository_version(self) -> None:
        from kilix_temps import __version__

        self.assertEqual(
            __version__,
            (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        )


if __name__ == "__main__":
    unittest.main()
