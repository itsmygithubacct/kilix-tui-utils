"""Shared telemetry adapters used by the CPU, memory, and thermal apps."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "memory"))
sys.path.insert(0, str(ROOT / "tools" / "temps"))

from kilix_memory.collect import snapshot_from_telemetry  # noqa: E402
from kilix_temps.sensors import (  # noqa: E402
    sample_from_telemetry,
    sensors_from_telemetry,
)

from kilix_tui import telemetry  # noqa: E402


def shared_snapshot() -> SimpleNamespace:
    system = SimpleNamespace(
        cpu_percent=37.5,
        load_1=1.0,
        load_5=0.5,
        load_15=0.25,
        logical_cpus=4,
        uptime_seconds=123.0,
        memory_total=8_000,
        memory_available=3_000,
        memory_free=1_000,
        memory_buffers=200,
        memory_cached=2_000,
        memory_reclaimable=300,
        memory_shared=100,
        memory_active=4_000,
        memory_inactive=2_000,
        memory_anon=3_500,
        memory_slab=400,
        memory_page_tables=50,
        memory_kernel_stack=25,
        memory_dirty=10,
        memory_writeback=5,
        swap_total=2_000,
        swap_free=1_500,
        memory_huge_total=8,
        memory_huge_free=3,
        memory_huge_page_size=2_097_152,
        memory_percent=62.5,
        pressure={
            "memory": {
                "some_avg10": 2.5,
                "some_avg60": 1.5,
                "some_avg300": 0.5,
                "some_total": 120,
                "full_avg10": 0.25,
                "full_avg60": 0.1,
                "full_avg300": 0.05,
                "full_total": 12,
            }
        },
        vm={
            "pgfault": 100,
            "pgmajfault": 3,
            "pswpin": 4,
            "pswpout": 5,
            "pgscan_kswapd": 6,
            "pgscan_direct": 7,
            "pgsteal_kswapd": 8,
            "oom_kill": 1,
            "allocstall_dma": 2,
            "compact_stall": 9,
        },
    )
    processes = (
        SimpleNamespace(
            pid=10,
            ppid=1,
            uid=1000,
            name="worker",
            state="R",
            threads=2,
            rss_bytes=500,
            virtual_bytes=2_000,
            anon_bytes=300,
            file_bytes=150,
            shared_bytes=50,
            command="worker --one",
            cpu_cores=0.75,
        ),
        SimpleNamespace(
            pid=11,
            ppid=1,
            uid=1000,
            name="worker",
            state="S",
            threads=1,
            rss_bytes=250,
            virtual_bytes=1_000,
            anon_bytes=100,
            file_bytes=100,
            shared_bytes=50,
            command="worker --two",
            cpu_cores=0.25,
        ),
    )
    thermal = (
        SimpleNamespace(
            key="cpu:0",
            chip="CPU",
            label="Package 0",
            source="hwmon0",
            celsius=72.5,
            warning_celsius=85.0,
            critical_celsius=100.0,
        ),
    )
    fans = (
        SimpleNamespace(
            key="fan:0",
            chip="ThinkPad",
            label="fan1",
            source="hwmon1",
            rpm=3200,
        ),
    )
    return SimpleNamespace(
        wall_time_ns=1_700_000_000_000_000_000,
        monotonic_ns=12_500_000_000,
        system=system,
        processes=processes,
        thermal=thermal,
        fans=fans,
    )


class SharedClientTests(unittest.TestCase):
    def test_client_start_is_rate_limited_and_direct_fallback_stays_in_app(self):
        client = mock.Mock()
        client.paths = object()
        starter = mock.Mock()
        package = SimpleNamespace(ensure_running=starter)
        expected = shared_snapshot()
        client.snapshot.return_value = expected
        with (
            mock.patch.object(telemetry, "_CLIENT", client),
            mock.patch.object(telemetry, "_package", return_value=package),
            mock.patch.object(telemetry, "_NEXT_START_ATTEMPT", 0.0),
            mock.patch("kilix_tui.telemetry.time.monotonic", side_effect=(1.0, 2.0)),
        ):
            self.assertIs(telemetry.snapshot(), expected)
            self.assertIs(telemetry.snapshot(), expected)
        self.assertEqual(
            client.snapshot.call_args_list,
            [
                mock.call(start=False, fallback=False, force=True),
                mock.call(start=False, fallback=False, force=True),
            ],
        )
        starter.assert_called_once_with(client.paths, timeout=0.0)


class ConsumerAdapterTests(unittest.TestCase):
    def test_memory_dashboard_maps_the_shared_record(self):
        sample = snapshot_from_telemetry(
            shared_snapshot(), hostname="test", user_name=lambda uid: f"u{uid}"
        )
        self.assertEqual(sample.memory.total, 8_000)
        self.assertEqual(sample.memory.huge_page_size, 2_097_152)
        self.assertEqual(sample.pressure.some.avg10, 2.5)
        self.assertEqual(sample.vm.page_scan, 13)
        self.assertEqual(sample.vm.page_steal, 8)
        self.assertEqual(sample.processes[0].user, "u1000")
        self.assertEqual(sample.processes[0].rss, 500)

    def test_temperature_dashboard_maps_sensors_fans_and_current_cpu(self):
        snapshot = shared_snapshot()
        temperatures, fans = sensors_from_telemetry(snapshot)
        sample = sample_from_telemetry(snapshot)
        self.assertEqual(temperatures[0].critical_hint, 100.0)
        self.assertEqual(fans[0].display_name, "ThinkPad / fan1")
        self.assertEqual(sample.temperatures, {"cpu:0": 72.5})
        self.assertEqual(sample.fans, {"fan:0": 3200})
        self.assertEqual(sample.metrics.cpu_percent, 37.5)
        self.assertEqual(sample.metrics.memory_percent, 62.5)
        self.assertEqual(sample.metrics.top_processes[0].name, "worker")
        self.assertEqual(sample.metrics.top_processes[0].cpu_percent, 100.0)
        self.assertEqual(sample.metrics.top_processes[0].instances, 2)


if __name__ == "__main__":
    unittest.main()
