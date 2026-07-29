from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import stat
import tempfile
import unittest

from kilix_temps.app import AppConfig, CsvLogger, DashboardApp
from kilix_temps.model import ThermalModel, ThresholdConfig
from kilix_temps.render import Renderer
from kilix_temps.sensors import FanSensor, Sample, SystemMetrics, TemperatureSensor
from kilix_temps.units import TemperatureUnit


class CsvLoggerTests(unittest.TestCase):
    def test_logger_writes_private_temperature_and_fan_rows(self) -> None:
        sensor = TemperatureSensor(
            "cpu", "CPU", "Package", "hwmon7", None, 100.0, 100.0
        )
        fan = FanSensor("fan", "ThinkPad", "fan1", "hwmon5", None)
        sample = Sample(
            timestamp=datetime(2026, 7, 21, tzinfo=timezone.utc),
            monotonic=1.0,
            temperatures={"cpu": 93.0},
            fans={"fan": 4500},
            metrics=SystemMetrics(None, 0.0, 0.0, 0.0, 8, 1.0, None),
        )
        model = ThermalModel(ThresholdConfig(), history_size=10)
        model.update(sample, [sensor])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state/temperatures.csv"
            logger = CsvLogger(path)
            logger.start()
            logger.write(sample, model, [fan])
            logger.close()

            contents = path.read_text(encoding="utf-8")
            self.assertIn("timestamp,kind,key", contents)
            self.assertIn("temperature,cpu,CPU,Package,93.000,celsius,HOT", contents)
            self.assertIn("fan,fan,ThinkPad,fan1,4500,rpm", contents)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_logger_throttles_samples_to_configured_interval(self) -> None:
        sensor = TemperatureSensor("cpu", "CPU", "Package", "demo", None)
        model = ThermalModel(ThresholdConfig(), history_size=10)
        first = Sample(
            timestamp=datetime(2026, 7, 21, tzinfo=timezone.utc),
            monotonic=1.0,
            temperatures={"cpu": 80.0},
            fans={},
            metrics=SystemMetrics(None, 0.0, 0.0, 0.0, 8, 1.0, None),
        )
        second = Sample(
            timestamp=datetime(2026, 7, 21, tzinfo=timezone.utc),
            monotonic=1.5,
            temperatures={"cpu": 81.0},
            fans={},
            metrics=first.metrics,
        )
        model.update(first, [sensor])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "temperatures.csv"
            logger = CsvLogger(path, minimum_interval=1.0)
            logger.start()
            logger.write(first, model, [])
            model.update(second, [sensor])
            logger.write(second, model, [])
            logger.close()
            rows = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)


class _HotBackend:
    def __init__(self) -> None:
        self.sensor = TemperatureSensor("cpu", "CPU", "Package", "demo", None)

    def discover(self):
        return [self.sensor], []

    def sample(self, temperatures, fans):
        del temperatures, fans
        return Sample(
            timestamp=datetime(2026, 7, 21, tzinfo=timezone.utc),
            monotonic=1.0,
            temperatures={"cpu": 95.0},
            fans={},
            metrics=SystemMetrics(None, 0.0, 0.0, 0.0, 8, 1.0, None),
        )


class DashboardAppTests(unittest.TestCase):
    def test_hot_startup_queues_bell_and_scroll_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = DashboardApp(
                _HotBackend(),
                ThermalModel(ThresholdConfig(), history_size=10),
                Renderer(False),
                AppConfig(0.5, False, Path(directory) / "log.csv"),
            )
            self.assertTrue(app._pending_bell)
            app.options.scroll = 999
            app._key("down")
            self.assertEqual(app.options.scroll, 0)

    def test_unit_key_toggles_display_without_changing_model_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = DashboardApp(
                _HotBackend(),
                ThermalModel(ThresholdConfig(), history_size=10),
                Renderer(False),
                AppConfig(0.5, False, Path(directory) / "log.csv"),
            )
            self.assertIs(
                app.options.temperature_unit,
                TemperatureUnit.FAHRENHEIT,
            )
            app._key("u")
            self.assertIs(
                app.options.temperature_unit,
                TemperatureUnit.CELSIUS,
            )
            self.assertEqual(app.model.states["cpu"].current, 95.0)


if __name__ == "__main__":
    unittest.main()
