from __future__ import annotations

import unittest

from kilix_memory.app import AppConfig, DashboardApp
from kilix_memory.collect import DemoMemoryBackend
from kilix_memory.model import MemoryModel
from kilix_memory.render import Renderer


class AppInputTests(unittest.TestCase):
    def setUp(self):
        self.app = DashboardApp(
            DemoMemoryBackend(),
            MemoryModel(20),
            Renderer(False),
            AppConfig(),
        )

    def test_navigation_and_modes(self):
        self.app._handle_input(b" s\x1b[B\x1b[6~")
        self.assertTrue(self.app.options.paused)
        self.assertEqual(self.app.options.sort_mode, "pid")
        self.assertEqual(self.app.options.scroll, 11)
        self.app._handle_input(b"h")
        self.assertTrue(self.app.options.help_visible)

    def test_quit(self):
        self.app._handle_input(b"q")
        self.assertFalse(self.app.running)


if __name__ == "__main__":
    unittest.main()
