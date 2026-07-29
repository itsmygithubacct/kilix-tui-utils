from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from kilix_memory.collect import (
    GIB,
    KIB,
    LinuxMemoryBackend,
    parse_meminfo,
    parse_pressure,
    parse_vmstat,
)


MEMINFO = """\
MemTotal:       16777216 kB
MemFree:         1048576 kB
MemAvailable:    6291456 kB
Buffers:          262144 kB
Cached:          4194304 kB
SwapCached:            0 kB
Active:          7340032 kB
Inactive:        3145728 kB
AnonPages:       5242880 kB
Shmem:            524288 kB
Slab:             786432 kB
SReclaimable:     393216 kB
KernelStack:       65536 kB
PageTables:       131072 kB
Dirty:              4096 kB
Writeback:             0 kB
SwapTotal:       4194304 kB
SwapFree:        3145728 kB
"""


class ParseTests(unittest.TestCase):
    def test_meminfo_uses_available_for_primary_usage(self):
        memory = parse_meminfo(MEMINFO)
        self.assertEqual(memory.total, 16 * GIB)
        self.assertEqual(memory.available, 6 * GIB)
        self.assertEqual(memory.used, 10 * GIB)
        self.assertEqual(memory.swap_used, GIB)
        self.assertAlmostEqual(memory.used_percent, 62.5)
        self.assertEqual(sum(value for _, value in memory.composition), memory.total)

    def test_meminfo_falls_back_without_memavailable(self):
        text = """\
MemTotal: 1000 kB
MemFree: 100 kB
Buffers: 50 kB
Cached: 300 kB
SReclaimable: 100 kB
Shmem: 20 kB
"""
        memory = parse_meminfo(text)
        self.assertEqual(memory.available, 530 * KIB)

    def test_pressure_and_vmstat(self):
        pressure = parse_pressure(
            "some avg10=1.25 avg60=0.50 avg300=0.10 total=1234\n"
            "full avg10=0.20 avg60=0.10 avg300=0.01 total=50\n"
        )
        self.assertTrue(pressure.supported)
        self.assertEqual(pressure.some.avg10, 1.25)
        self.assertEqual(pressure.full.total_us, 50)
        vm = parse_vmstat(
            "pgfault 100\npgmajfault 3\npswpin 4\npswpout 5\n"
            "pgscan_kswapd 6\npgscan_direct 7\n"
            "pgsteal_kswapd 8\noom_kill 1\nallocstall_dma 2\n"
        )
        self.assertEqual(vm.page_scan, 13)
        self.assertEqual(vm.page_steal, 8)
        self.assertEqual(vm.alloc_stalls, 2)

    def test_vmstat_does_not_double_count_reclaim_breakdowns(self):
        vm = parse_vmstat(
            "pgscan_kswapd 60\npgscan_direct 40\n"
            "pgscan_anon 25\npgscan_file 75\n"
            "pgsteal_kswapd 50\npgsteal_direct 30\n"
            "pgsteal_anon 20\npgsteal_file 60\n"
        )
        self.assertEqual(vm.page_scan, 100)
        self.assertEqual(vm.page_steal, 80)


class BackendTests(unittest.TestCase):
    def test_fake_proc_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            (proc / "pressure").mkdir(parents=True)
            (proc / "meminfo").write_text(MEMINFO)
            (proc / "vmstat").write_text("pgfault 12\npgmajfault 1\n")
            (proc / "pressure" / "memory").write_text(
                "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            )
            process = proc / "321"
            process.mkdir()
            process.joinpath("status").write_text(
                "Name:\tworker\nState:\tS (sleeping)\nPPid:\t12\n"
                "Uid:\t0\t0\t0\t0\nThreads:\t3\n"
                "VmRSS:\t2048 kB\nVmSize:\t8192 kB\n"
                "RssAnon:\t1024 kB\nRssFile:\t768 kB\nRssShmem:\t256 kB\n"
            )
            process.joinpath("cmdline").write_bytes(b"python3\0worker.py\0")
            snapshot = LinuxMemoryBackend(root).sample()
            self.assertEqual(len(snapshot.processes), 1)
            row = snapshot.processes[0]
            self.assertEqual(row.pid, 321)
            self.assertEqual(row.rss, 2 * 1024 * 1024)
            self.assertEqual(row.command, "python3 worker.py")


if __name__ == "__main__":
    unittest.main()
