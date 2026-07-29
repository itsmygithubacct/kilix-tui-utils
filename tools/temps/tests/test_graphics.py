from __future__ import annotations

from datetime import datetime, timezone
import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kilix_temps.graphics import (
    GraphicalRenderer,
    GraphicsFrame,
    KittyDisplay,
    graphics_available,
    kitty_graphics_likely,
    terminal_pixel_size,
)
from kilix_temps.model import ThermalModel, ThresholdConfig
from kilix_temps.render import FrameOptions
from kilix_temps.sensors import (
    FanSensor,
    ProcessLoad,
    Sample,
    SystemMetrics,
    TemperatureSensor,
)
from kilix_temps.units import TemperatureUnit


class GraphicsRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sensor = TemperatureSensor(
            "cpu", "CPU", "Package 0", "hwmon7", None, 80.0, 100.0
        )
        self.fan = FanSensor("fan", "ThinkPad", "fan1", "hwmon5", None)
        self.sample = Sample(
            timestamp=datetime(2026, 7, 22, 14, 30, tzinfo=timezone.utc),
            monotonic=60.0,
            temperatures={"cpu": 91.5},
            fans={"fan": 4_800},
            metrics=SystemMetrics(
                82.0,
                5.5,
                3.2,
                2.0,
                8,
                90_000.0,
                61.0,
                (ProcessLoad("ninja", 175.0, 8),),
            ),
        )
        self.model = ThermalModel(ThresholdConfig(), history_size=30)
        for index, value in enumerate((70.0, 73.0, 78.0, 84.0, 91.5)):
            sample = Sample(
                timestamp=self.sample.timestamp,
                monotonic=float(index * 15),
                temperatures={"cpu": value},
                fans=self.sample.fans,
                metrics=self.sample.metrics,
            )
            self.model.update(sample, [self.sensor])

    def test_graphics_backend_is_soft_raster(self) -> None:
        available, reason = graphics_available()
        self.assertTrue(available, reason)
        renderer = GraphicalRenderer()
        self.assertEqual(renderer._soft_raster.__name__, "soft_raster")
        self.assertEqual(renderer._library.abi_version, (0, 3))

    def test_wide_frame_has_exact_rgb_geometry_and_visual_range(self) -> None:
        frame = GraphicalRenderer().render(
            self.model,
            self.sample,
            [self.fan],
            120,
            36,
            FrameOptions(),
            pixel_size=(1_200, 720),
        )
        self.assertEqual((frame.width, frame.height), (1_200, 720))
        self.assertEqual((frame.columns, frame.rows), (120, 36))
        self.assertEqual(len(frame.rgb), 1_200 * 720 * 3)
        self.assertGreater(len(set(frame.rgb[::97])), 25)

    def test_compact_and_help_frames_are_supported(self) -> None:
        renderer = GraphicalRenderer()
        compact = renderer.render(
            self.model,
            self.sample,
            [self.fan],
            48,
            14,
            FrameOptions(),
            pixel_size=(480, 280),
        )
        help_frame = renderer.render(
            self.model,
            self.sample,
            [self.fan],
            90,
            30,
            FrameOptions(help_visible=True),
            pixel_size=(900, 600),
        )
        self.assertEqual(len(compact.rgb), 480 * 280 * 3)
        self.assertEqual(len(help_frame.rgb), 900 * 600 * 3)
        self.assertNotEqual(compact.rgb[:10_000], help_frame.rgb[:10_000])

    def test_temperature_unit_changes_pixel_dashboard(self) -> None:
        renderer = GraphicalRenderer()
        fahrenheit = renderer.render(
            self.model,
            self.sample,
            [self.fan],
            48,
            14,
            FrameOptions(temperature_unit=TemperatureUnit.FAHRENHEIT),
            pixel_size=(480, 280),
        )
        celsius = renderer.render(
            self.model,
            self.sample,
            [self.fan],
            48,
            14,
            FrameOptions(temperature_unit=TemperatureUnit.CELSIUS),
            pixel_size=(480, 280),
        )
        self.assertNotEqual(fahrenheit.rgb, celsius.rgb)


class GraphicsTransportTests(unittest.TestCase):
    def test_terminal_detection_uses_kilix_and_known_implementations(self) -> None:
        self.assertTrue(kitty_graphics_likely({"KITTY_WINDOW_ID": "3"}))
        self.assertTrue(kitty_graphics_likely({"KILIX_STREAM": "1"}))
        self.assertTrue(kitty_graphics_likely({"TERM_PROGRAM": "ghostty"}))
        self.assertFalse(kitty_graphics_likely({"TERM": "xterm-256color"}))

    def test_pixel_geometry_falls_back_to_cell_dimensions(self) -> None:
        with patch("kilix_temps.graphics.fcntl.ioctl", side_effect=OSError):
            self.assertEqual(terminal_pixel_size(1, 80, 24), (800, 480))

    def test_display_forwards_frames_and_removes_its_image(self) -> None:
        events: list[object] = []

        class FakePresenter:
            next_deadline = None

            def __init__(self, terminal, **settings):
                events.append(("init", terminal, settings))

            def present(self, *args, **settings):
                events.append(("present", args, settings))
                return SimpleNamespace(emitted=True)

            def invalidate(self):
                events.append("invalidate")

            def flush(self):
                events.append("flush")
                return SimpleNamespace(emitted=False)

            def close(self):
                events.append("close")

        module = SimpleNamespace(
            FramePresenter=FakePresenter,
            wrap_tmux_passthrough=lambda value: "wrapped:" + value,
        )
        output = io.StringIO()
        environment = {key: value for key, value in os.environ.items() if key not in {"TMUX", "KILIX_STREAM"}}
        with patch.dict(os.environ, environment, clear=True), patch(
            "kilix_temps.graphics._presenter_module", return_value=module
        ):
            display = KittyDisplay(output, presenter_class=FakePresenter)
            frame = GraphicsFrame(b"\x00\x01\x02" * 4, 2, 2, 10, 5)
            display.invalidate()
            display.present(frame, force_full=True)
            display.flush()
            display.close()

        self.assertIn("invalidate", events)
        self.assertIn("flush", events)
        self.assertEqual(events[-1], "close")
        self.assertIn("a=d,d=I", output.getvalue())
        present = next(event for event in events if isinstance(event, tuple) and event[0] == "present")
        self.assertEqual(present[1][:5], (frame.rgb, 2, 2, 10, 5))
        self.assertTrue(present[2]["force_full"])


if __name__ == "__main__":
    unittest.main()
