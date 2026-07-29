from __future__ import annotations

from dataclasses import replace
import unittest

from kilix_memory.collect import DemoMemoryBackend
from kilix_memory.model import MemoryModel


class MemoryModelTests(unittest.TestCase):
    def test_rates_and_process_ordering(self):
        backend = DemoMemoryBackend()
        first = backend.sample()
        second = replace(
            first,
            monotonic=first.monotonic + 2.0,
            vm=replace(
                first.vm,
                page_faults=first.vm.page_faults + 200,
                major_faults=first.vm.major_faults + 6,
                swap_in=first.vm.swap_in + 10,
                oom_kills=first.vm.oom_kills + 1,
            ),
        )
        model = MemoryModel(20)
        model.update(first)
        model.update(second)
        self.assertEqual(model.rates.faults_per_second, 100)
        self.assertEqual(model.rates.major_faults_per_second, 3)
        self.assertEqual(model.rates.oom_kills_delta, 1)
        rss = model.ordered_processes("rss")
        self.assertGreaterEqual(rss[0].rss, rss[1].rss)
        pid = model.ordered_processes("pid")
        self.assertLess(pid[0].pid, pid[-1].pid)
        self.assertEqual(len(model.history), 2)

    def test_reset_keeps_current_sample(self):
        model = MemoryModel(20)
        model.update(DemoMemoryBackend().sample())
        model.reset_history()
        self.assertIsNotNone(model.current)
        self.assertEqual(len(model.history), 1)


if __name__ == "__main__":
    unittest.main()
