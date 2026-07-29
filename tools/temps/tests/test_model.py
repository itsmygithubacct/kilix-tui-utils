from __future__ import annotations

from datetime import datetime, timezone
import unittest

from kilix_temps.model import Level, ThermalModel, ThresholdConfig, level_for, policy_for
from kilix_temps.sensors import Sample, SystemMetrics, TemperatureSensor


METRICS = SystemMetrics(None, 0.0, 0.0, 0.0, 8, 10.0, 20.0)


def sample(key: str, value: float, monotonic: float) -> Sample:
    return Sample(
        timestamp=datetime.now(timezone.utc),
        monotonic=monotonic,
        temperatures={key: value},
        fans={},
        metrics=METRICS,
    )


class ThermalModelTests(unittest.TestCase):
    def test_lower_hardware_limit_wins(self) -> None:
        sensor = TemperatureSensor(
            "nvme", "NVMe", "Composite", "hwmon2", None, 81.8, 84.8
        )
        policy = policy_for(sensor, ThresholdConfig())
        self.assertEqual(policy.critical, 84.8)
        self.assertAlmostEqual(policy.hot, 81.8)
        self.assertAlmostEqual(policy.warning, 78.8)
        self.assertEqual(level_for(82.0, policy), Level.HOT)

    def test_escalation_creates_alert_and_tracks_trend(self) -> None:
        sensor = TemperatureSensor(
            "cpu", "CPU", "Package 0", "hwmon7", None, 100.0, 100.0
        )
        model = ThermalModel(ThresholdConfig(), history_size=20)
        self.assertEqual(model.update(sample("cpu", 70.0, 10.0), [sensor]), [])
        alerts = model.update(sample("cpu", 92.0, 20.0), [sensor])
        self.assertEqual(len(alerts), 1)
        state = model.states["cpu"]
        self.assertEqual(state.level, Level.HOT)
        self.assertAlmostEqual(state.maximum or 0.0, 92.0)
        self.assertAlmostEqual(state.trend_per_minute or 0.0, 132.0)
        self.assertIs(model.most_at_risk, state)

    def test_reset_peaks_keeps_current_value(self) -> None:
        sensor = TemperatureSensor("cpu", "CPU", "Package", "demo", None)
        model = ThermalModel(ThresholdConfig(), history_size=20)
        model.update(sample("cpu", 72.0, 1.0), [sensor])
        model.update(sample("cpu", 88.0, 3.0), [sensor])
        model.reset_peaks()
        state = model.states["cpu"]
        self.assertEqual(state.minimum, 88.0)
        self.assertEqual(state.maximum, 88.0)
        self.assertEqual(state.values, [88.0])

    def test_level_downgrade_uses_hysteresis(self) -> None:
        sensor = TemperatureSensor("cpu", "CPU", "Package", "demo", None)
        model = ThermalModel(ThresholdConfig(), history_size=20)
        model.update(sample("cpu", 92.0, 1.0), [sensor])
        model.update(sample("cpu", 89.0, 2.0), [sensor])
        self.assertEqual(model.states["cpu"].level, Level.HOT)
        model.update(sample("cpu", 88.0, 3.0), [sensor])
        self.assertEqual(model.states["cpu"].level, Level.WARM)
        self.assertEqual(len(model.alerts), 1)

    def test_trend_uses_recent_thirty_second_window(self) -> None:
        sensor = TemperatureSensor("cpu", "CPU", "Package", "demo", None)
        model = ThermalModel(ThresholdConfig(), history_size=20)
        model.update(sample("cpu", 40.0, 0.0), [sensor])
        model.update(sample("cpu", 70.0, 100.0), [sensor])
        model.update(sample("cpu", 90.0, 120.0), [sensor])
        self.assertAlmostEqual(model.states["cpu"].trend_per_minute or 0.0, 60.0)


if __name__ == "__main__":
    unittest.main()
