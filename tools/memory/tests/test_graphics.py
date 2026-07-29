from __future__ import annotations

import unittest

from kilix_memory.collect import DemoMemoryBackend
from kilix_memory.graphics import GraphicalRenderer, kitty_graphics_likely
from kilix_memory.model import MemoryModel
from kilix_memory.render import FrameOptions


class GraphicsTests(unittest.TestCase):
    def setUp(self):
        self.model = MemoryModel(20)
        self.model.update(DemoMemoryBackend().sample())
        self.renderer = GraphicalRenderer()

    def test_environment_detection(self):
        self.assertTrue(kitty_graphics_likely({"KITTY_WINDOW_ID": "4"}))
        self.assertTrue(kitty_graphics_likely({"TERM_PROGRAM": "WezTerm"}))
        self.assertFalse(kitty_graphics_likely({"TERM": "xterm-256color"}))

    def test_wide_and_compact_frames(self):
        wide = self.renderer.render(
            self.model,
            100,
            34,
            FrameOptions(),
            pixel_size=(1000, 680),
        )
        compact = self.renderer.render(
            self.model,
            50,
            18,
            FrameOptions(),
            pixel_size=(500, 360),
        )
        self.assertEqual(len(wide.rgb), 1000 * 680 * 3)
        self.assertEqual(len(compact.rgb), 500 * 360 * 3)
        self.assertNotEqual(wide.rgb[:3000], b"\0" * 3000)


if __name__ == "__main__":
    unittest.main()
