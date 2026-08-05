"""The shared freedesktop scan reads real files the way the spec says to.

This is the discovery layer Kilix 95's Start menu already trusts, carried
into the shared core; what is pinned here is the contract every consumer
relies on — spec-compliant skipping, field-code stripping, precedence, and
bucketing — against real files in a temporary XDG tree.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kilix_tui import xdgapps  # noqa: E402


def write_desktop(root: Path, name: str, body: str) -> None:
    apps = root / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / name).write_text(body, encoding="utf-8")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.user = base / "user"
        self.system = base / "system"
        self.user.mkdir()
        self.system.mkdir()
        patcher = mock.patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.user),
            "XDG_DATA_DIRS": str(self.system),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def scan(self):
        return xdgapps.scan(force=True)

    def test_an_application_parses_with_field_codes_stripped(self):
        write_desktop(self.system, "browser.desktop", "\n".join([
            "[Desktop Entry]",
            "Type=Application",
            "Name=Browser",
            "Exec=browser %U --new-window",
            "Categories=Network;WebBrowser;",
        ]))
        entries = self.scan()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["exec"], "browser --new-window")
        self.assertFalse(entries[0]["terminal"])
        self.assertEqual(xdgapps.bucket(entries[0]), "Internet")

    def test_nodisplay_and_hidden_entries_are_skipped(self):
        write_desktop(self.system, "hidden.desktop", "\n".join([
            "[Desktop Entry]", "Type=Application", "Name=H",
            "Exec=h", "NoDisplay=true",
        ]))
        write_desktop(self.system, "gone.desktop", "\n".join([
            "[Desktop Entry]", "Type=Application", "Name=G",
            "Exec=g", "Hidden=true",
        ]))
        self.assertEqual(self.scan(), [])

    def test_tryexec_filters_missing_programs(self):
        write_desktop(self.system, "ghost.desktop", "\n".join([
            "[Desktop Entry]", "Type=Application", "Name=Ghost",
            "Exec=ghost", "TryExec=/nonexistent/ghost",
        ]))
        self.assertEqual(self.scan(), [])

    def test_the_user_directory_wins_the_same_id(self):
        write_desktop(self.system, "tool.desktop", "\n".join([
            "[Desktop Entry]", "Type=Application", "Name=System Tool",
            "Exec=tool",
        ]))
        write_desktop(self.user, "tool.desktop", "\n".join([
            "[Desktop Entry]", "Type=Application", "Name=My Tool",
            "Exec=tool --mine",
        ]))
        entries = self.scan()
        self.assertEqual([e["name"] for e in entries], ["My Tool"])

    def test_terminal_flag_and_grouping_order(self):
        write_desktop(self.system, "mon.desktop", "\n".join([
            "[Desktop Entry]", "Type=Application", "Name=Monitor",
            "Exec=mon", "Terminal=true", "Categories=Utility;",
        ]))
        write_desktop(self.system, "paint.desktop", "\n".join([
            "[Desktop Entry]", "Type=Application", "Name=Paint",
            "Exec=paint", "Categories=Graphics;",
        ]))
        self.scan()
        groups = xdgapps.grouped()
        self.assertEqual(list(groups), ["Accessories", "Graphics"])
        self.assertTrue(groups["Accessories"][0]["terminal"])

    def test_non_application_types_are_skipped(self):
        write_desktop(self.system, "link.desktop", "\n".join([
            "[Desktop Entry]", "Type=Link", "Name=A Link",
            "URL=https://example.org",
        ]))
        self.assertEqual(self.scan(), [])


if __name__ == "__main__":
    unittest.main()
