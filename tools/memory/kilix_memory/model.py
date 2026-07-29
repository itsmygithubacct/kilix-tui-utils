"""History, rates, and ordering for the memory dashboard."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os

from .collect import MemorySnapshot, ProcessMemory, VmCounters


@dataclass(frozen=True, slots=True)
class MemoryRates:
    faults_per_second: float = 0.0
    major_faults_per_second: float = 0.0
    swap_in_bytes_per_second: float = 0.0
    swap_out_bytes_per_second: float = 0.0
    scan_pages_per_second: float = 0.0
    steal_pages_per_second: float = 0.0
    oom_kills_delta: int = 0
    alloc_stalls_per_second: float = 0.0
    compact_stalls_per_second: float = 0.0


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    monotonic: float
    used_percent: float
    available_bytes: int
    swap_percent: float
    pressure_some: float
    pressure_full: float
    major_faults_per_second: float
    swap_io_bytes_per_second: float


def _delta(current: int, previous: int) -> int:
    # Kernel counters can reset after suspend, namespace changes, or overflow.
    return max(0, current - previous)


class MemoryModel:
    def __init__(self, history_size: int = 240) -> None:
        if history_size < 10:
            raise ValueError("history_size must be at least 10")
        self.history: deque[HistoryPoint] = deque(maxlen=history_size)
        self.current: MemorySnapshot | None = None
        self.previous: MemorySnapshot | None = None
        self.rates = MemoryRates()
        try:
            self.page_size = max(1, int(os.sysconf("SC_PAGE_SIZE")))
        except (OSError, ValueError):
            self.page_size = 4096

    @staticmethod
    def _counter_rates(
        current: VmCounters,
        previous: VmCounters,
        elapsed: float,
        page_size: int,
    ) -> MemoryRates:
        scale = 1.0 / max(0.001, elapsed)
        return MemoryRates(
            faults_per_second=_delta(
                current.page_faults, previous.page_faults
            ) * scale,
            major_faults_per_second=_delta(
                current.major_faults, previous.major_faults
            ) * scale,
            swap_in_bytes_per_second=_delta(
                current.swap_in, previous.swap_in
            ) * page_size * scale,
            swap_out_bytes_per_second=_delta(
                current.swap_out, previous.swap_out
            ) * page_size * scale,
            scan_pages_per_second=_delta(
                current.page_scan, previous.page_scan
            ) * scale,
            steal_pages_per_second=_delta(
                current.page_steal, previous.page_steal
            ) * scale,
            oom_kills_delta=_delta(current.oom_kills, previous.oom_kills),
            alloc_stalls_per_second=_delta(
                current.alloc_stalls, previous.alloc_stalls
            ) * scale,
            compact_stalls_per_second=_delta(
                current.compact_stalls, previous.compact_stalls
            ) * scale,
        )

    def update(self, snapshot: MemorySnapshot) -> None:
        previous = self.current
        self.previous = previous
        self.current = snapshot
        if previous is not None:
            elapsed = snapshot.monotonic - previous.monotonic
            if elapsed > 0:
                self.rates = self._counter_rates(
                    snapshot.vm, previous.vm, elapsed, self.page_size
                )
        memory = snapshot.memory
        self.history.append(
            HistoryPoint(
                monotonic=snapshot.monotonic,
                used_percent=memory.used_percent,
                available_bytes=memory.available,
                swap_percent=memory.swap_percent,
                pressure_some=snapshot.pressure.some.avg10,
                pressure_full=snapshot.pressure.full.avg10,
                major_faults_per_second=self.rates.major_faults_per_second,
                swap_io_bytes_per_second=(
                    self.rates.swap_in_bytes_per_second
                    + self.rates.swap_out_bytes_per_second
                ),
            )
        )

    def reset_history(self) -> None:
        current = self.current
        self.history.clear()
        self.previous = None
        self.rates = MemoryRates()
        if current is not None:
            self.update(current)

    def ordered_processes(
        self,
        sort_mode: str = "rss",
        query: str = "",
    ) -> list[ProcessMemory]:
        if self.current is None:
            return []
        rows = list(self.current.processes)
        if query:
            needle = query.casefold()
            rows = [
                process
                for process in rows
                if needle in process.name.casefold()
                or needle in process.command.casefold()
                or needle in process.user.casefold()
                or needle == str(process.pid)
            ]
        if sort_mode == "pid":
            return sorted(rows, key=lambda process: process.pid)
        if sort_mode == "name":
            return sorted(
                rows,
                key=lambda process: (
                    process.name.casefold(),
                    -process.rss,
                    process.pid,
                ),
            )
        if sort_mode == "user":
            return sorted(
                rows,
                key=lambda process: (
                    process.user.casefold(),
                    -process.rss,
                    process.pid,
                ),
            )
        return sorted(
            rows,
            key=lambda process: (-process.rss, process.pid),
        )

    @property
    def top_rss_total(self) -> int:
        return sum(process.rss for process in self.ordered_processes()[:10])
