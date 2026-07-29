from __future__ import annotations

import unittest

from kilix_memory.collect import DemoMemoryBackend, GIB
from kilix_memory.model import MemoryModel
from kilix_memory.render import (
    FrameOptions,
    Renderer,
    display_text,
    format_bytes,
    sparkline,
    strip_ansi,
)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.model = MemoryModel(20)
        self.model.update(DemoMemoryBackend().sample())

    def test_formatting(self):
        self.assertEqual(format_bytes(GIB), "1.00GiB")
        self.assertEqual(format_bytes(100 * GIB, short=True), "100G")
        self.assertEqual(len(sparkline([0, 1, 2, 3], 8)), 8)
        self.assertEqual(
            display_text("worker\x1b]2;spoofed\x07\nname"),
            "worker ]2;spoofed  name",
        )

    def test_text_frame_is_bounded_and_contains_processes(self):
        frame = Renderer(False).render(
            self.model, 100, 28, FrameOptions(sort_mode="rss")
        )
        lines = frame.splitlines()
        self.assertLessEqual(len(lines), 28)
        self.assertTrue(all(len(strip_ansi(line)) == 100 for line in lines))
        self.assertIn("KILIX // MEMORY", frame)
        self.assertIn("firefox", frame)

    def test_help(self):
        frame = Renderer(False).render(
            self.model, 80, 24, FrameOptions(help_visible=True)
        )
        self.assertIn("HELP", frame)
        self.assertIn("Monitoring only", frame)


if __name__ == "__main__":
    unittest.main()
