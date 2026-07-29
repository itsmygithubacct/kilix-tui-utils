from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from kilix_temps.sensors import SensorBackend, _parse_process_stat


class SensorBackendTests(unittest.TestCase):
    def write(self, root: Path, relative: str, value: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def test_discovers_thermal_hwmon_and_fans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "sys/class/thermal/thermal_zone0/type", "acpitz\n")
            self.write(root, "sys/class/thermal/thermal_zone0/temp", "95000\n")
            self.write(
                root,
                "sys/class/thermal/thermal_zone0/trip_point_0_type",
                "critical\n",
            )
            self.write(
                root,
                "sys/class/thermal/thermal_zone0/trip_point_0_temp",
                "128000\n",
            )
            self.write(root, "sys/class/hwmon/hwmon7/name", "coretemp\n")
            self.write(root, "sys/class/hwmon/hwmon7/temp1_input", "93000\n")
            self.write(root, "sys/class/hwmon/hwmon7/temp1_label", "Package id 0\n")
            self.write(root, "sys/class/hwmon/hwmon7/temp1_max", "100000\n")
            self.write(root, "sys/class/hwmon/hwmon7/temp1_crit", "100000\n")
            self.write(root, "sys/class/hwmon/hwmon5/name", "thinkpad\n")
            self.write(root, "sys/class/hwmon/hwmon5/fan1_input", "4411\n")
            self.write(root, "sys/class/hwmon/hwmon5/temp3_input", "0\n")

            backend = SensorBackend(root)
            temperatures, fans = backend.discover()
            sample = backend.sample(temperatures, fans)

            self.assertEqual(len(temperatures), 3)
            self.assertEqual(len(fans), 1)
            self.assertEqual(len(sample.temperatures), 2)
            self.assertEqual(next(iter(sample.fans.values())), 4411)
            zone = next(sensor for sensor in temperatures if sensor.source == "thermal-zone")
            package = next(sensor for sensor in temperatures if sensor.chip == "CPU")
            self.assertEqual(zone.critical_hint, 128.0)
            self.assertEqual(package.label, "Package 0")
            self.assertEqual(sample.temperatures[package.key], 93.0)

    def test_invalid_trip_points_and_temperatures_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "sys/class/thermal/thermal_zone2/type", "iwlwifi_1\n")
            self.write(root, "sys/class/thermal/thermal_zone2/temp", "51000\n")
            self.write(
                root,
                "sys/class/thermal/thermal_zone2/trip_point_0_type",
                "passive\n",
            )
            self.write(
                root,
                "sys/class/thermal/thermal_zone2/trip_point_0_temp",
                "-32768000\n",
            )
            backend = SensorBackend(root)
            temperatures, fans = backend.discover()
            sample = backend.sample(temperatures, fans)
            self.assertIsNone(temperatures[0].warning_hint)
            self.assertEqual(sample.temperatures[temperatures[0].key], 51.0)

    def test_process_stat_parser_handles_spaces_and_parentheses(self) -> None:
        fields = ["R"] + [str(value) for value in range(1, 11)] + ["120", "30"]
        parsed = _parse_process_stat("123 (worker (hot path)) " + " ".join(fields))
        self.assertEqual(parsed, ("worker (hot path)", 150))

    def test_sensor_labels_cannot_inject_terminal_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "sys/class/hwmon/hwmon0/name", "chip\x1b[31m\n")
            self.write(root, "sys/class/hwmon/hwmon0/temp1_label", "hot\x07label\n")
            self.write(root, "sys/class/hwmon/hwmon0/temp1_input", "42000\n")
            temperatures, _ = SensorBackend(root).discover()
            self.assertNotIn("\x1b", temperatures[0].chip)
            self.assertNotIn("\x07", temperatures[0].label)


if __name__ == "__main__":
    unittest.main()
