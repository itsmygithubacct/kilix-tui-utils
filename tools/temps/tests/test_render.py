from __future__ import annotations

from datetime import datetime, timezone
import unittest

from kilix_temps.model import ThermalModel, ThresholdConfig
from kilix_temps.render import FrameOptions, Renderer, strip_ansi, visible_len
from kilix_temps.sensors import (
    FanSensor,
    ProcessLoad,
    Sample,
    SystemMetrics,
    TemperatureSensor,
)
from kilix_temps.units import TemperatureUnit


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sensor = TemperatureSensor(
            "cpu", "CPU", "Package 0", "hwmon7", None, 100.0, 100.0
        )
        self.fan = FanSensor("fan1", "ThinkPad", "fan1", "hwmon5", None)
        self.sample = Sample(
            timestamp=datetime(2026, 7, 21, 0, 15, tzinfo=timezone.utc),
            monotonic=100.0,
            temperatures={"cpu": 93.0},
            fans={"fan1": 4411},
            metrics=SystemMetrics(
                87.5,
                7.0,
                4.0,
                2.0,
                8,
                1200.0,
                35.0,
                (ProcessLoad("dosbox-debug", 188.0, 4),),
            ),
        )
        self.model = ThermalModel(ThresholdConfig(), history_size=20)
        self.model.update(self.sample, [self.sensor])

    def test_plain_frame_is_bounded_and_contains_safety_state(self) -> None:
        width, height = 100, 24
        frame = Renderer(False).render(
            self.model,
            self.sample,
            [self.fan],
            width,
            height,
            FrameOptions(),
        )
        lines = frame.splitlines()
        self.assertEqual(len(lines), height)
        self.assertTrue(all(len(line) <= width for line in lines))
        self.assertIn("KILIX TUI", frame)
        self.assertIn("▶1 Monitor", frame.splitlines()[1])
        self.assertNotIn("//", frame)
        self.assertIn("HOT", frame)
        self.assertIn("CPU / Package 0", frame)
        self.assertIn("199.4°F", frame)
        self.assertIn("4,411 RPM", frame)
        self.assertIn("dosbox-debug ×4 188%", frame)

    def test_celsius_display_can_be_selected(self) -> None:
        frame = Renderer(False).render(
            self.model,
            self.sample,
            [self.fan],
            100,
            24,
            FrameOptions(temperature_unit=TemperatureUnit.CELSIUS),
        )
        self.assertIn("93.0°C", frame)
        self.assertNotIn("199.4°F", frame)

    def test_ansi_width_helpers_ignore_color_sequences(self) -> None:
        value = "\x1b[31mHOT\x1b[0m"
        self.assertEqual(strip_ansi(value), "HOT")
        self.assertEqual(visible_len(value), 3)

    def test_help_fills_requested_height(self) -> None:
        options = FrameOptions(help_visible=True)
        frame = Renderer(False).render(
            self.model, self.sample, [self.fan], 90, 25, options
        )
        self.assertEqual(len(frame.splitlines()), 25)
        self.assertIn("Alert policy", frame)
        self.assertIn("Monitoring only", frame)

    def test_tiny_pane_gets_non_wrapping_size_message(self) -> None:
        frame = Renderer(False).render(
            self.model, self.sample, [self.fan], 28, 6, FrameOptions()
        )
        lines = frame.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertTrue(all(len(line) == 28 for line in lines))
        self.assertIn("pane needs 40 x 10", frame)

    def test_representative_sizes_never_exceed_pane(self) -> None:
        for color in (False, True):
            renderer = Renderer(color)
            for width in (40, 41, 57, 58, 81, 82, 117, 118, 160):
                for height in (10, 16, 22, 30):
                    with self.subTest(color=color, width=width, height=height):
                        frame = renderer.render(
                            self.model,
                            self.sample,
                            [self.fan],
                            width,
                            height,
                            FrameOptions(),
                        )
                        lines = frame.splitlines()
                        self.assertEqual(len(lines), height)
                        self.assertTrue(
                            all(visible_len(line) <= width for line in lines)
                        )


if __name__ == "__main__":
    unittest.main()
