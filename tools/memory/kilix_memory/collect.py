"""Read-only Linux memory collection from procfs.

The dashboard intentionally uses the kernel interfaces directly.  This keeps
the one-shot CLI useful on small systems and avoids a runtime dependency on
psutil while retaining an injectable root for deterministic tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import pwd
import socket
import time


KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class MemoryStats:
    total: int
    available: int
    free: int
    buffers: int
    cached: int
    reclaimable: int
    shmem: int
    active: int
    inactive: int
    anon: int
    slab: int
    page_tables: int
    kernel_stack: int
    dirty: int
    writeback: int
    swap_total: int
    swap_free: int
    huge_total: int = 0
    huge_free: int = 0
    huge_page_size: int = 0

    @property
    def used(self) -> int:
        """Memory not immediately available to new allocations."""
        return max(0, self.total - min(self.total, self.available))

    @property
    def used_percent(self) -> float:
        return self.used * 100.0 / self.total if self.total else 0.0

    @property
    def available_percent(self) -> float:
        return self.available * 100.0 / self.total if self.total else 0.0

    @property
    def swap_used(self) -> int:
        return max(0, self.swap_total - min(self.swap_total, self.swap_free))

    @property
    def swap_percent(self) -> float:
        return self.swap_used * 100.0 / self.swap_total if self.swap_total else 0.0

    @property
    def cache_bytes(self) -> int:
        # Cached includes tmpfs/Shmem; subtracting Shmem prevents showing the
        # same resident pages as both shared/application memory and cache.
        return max(0, self.cached + self.reclaimable - self.shmem)

    @property
    def application_bytes(self) -> int:
        """Non-free, non-buffer, non-reclaimable physical memory."""
        return max(
            0,
            self.total - self.free - self.buffers - self.cache_bytes,
        )

    @property
    def composition(self) -> tuple[tuple[str, int], ...]:
        """A bounded, additive physical-RAM composition."""
        remaining = self.total
        values: list[tuple[str, int]] = []
        for label, raw in (
            ("apps", self.application_bytes),
            ("cache", self.cache_bytes),
            ("buffers", self.buffers),
            ("free", self.free),
        ):
            value = _clamp(raw, 0, remaining)
            values.append((label, value))
            remaining -= value
        if remaining:
            label, value = values[0]
            values[0] = (label, value + remaining)
        return tuple(values)


@dataclass(frozen=True, slots=True)
class PressureLine:
    avg10: float = 0.0
    avg60: float = 0.0
    avg300: float = 0.0
    total_us: int = 0


@dataclass(frozen=True, slots=True)
class MemoryPressure:
    some: PressureLine = PressureLine()
    full: PressureLine = PressureLine()
    supported: bool = False


@dataclass(frozen=True, slots=True)
class VmCounters:
    page_faults: int = 0
    major_faults: int = 0
    swap_in: int = 0
    swap_out: int = 0
    page_scan: int = 0
    page_steal: int = 0
    oom_kills: int = 0
    alloc_stalls: int = 0
    compact_stalls: int = 0


@dataclass(frozen=True, slots=True)
class ProcessMemory:
    pid: int
    ppid: int
    uid: int
    user: str
    name: str
    state: str
    threads: int
    rss: int
    virtual: int
    anon: int
    file: int
    shared: int
    command: str

    def rss_percent(self, total: int) -> float:
        return self.rss * 100.0 / total if total else 0.0


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    timestamp: datetime
    monotonic: float
    hostname: str
    memory: MemoryStats
    pressure: MemoryPressure
    vm: VmCounters
    processes: tuple[ProcessMemory, ...]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_kib_document(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, tail = line.partition(":")
        if not separator:
            continue
        fields = tail.split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError:
            continue
        multiplier = KIB if len(fields) > 1 and fields[1].lower() == "kb" else 1
        values[key] = max(0, value * multiplier)
    return values


def parse_meminfo(text: str) -> MemoryStats:
    values = _parse_kib_document(text)
    total = values.get("MemTotal", 0)
    if total <= 0:
        raise ValueError("/proc/meminfo did not contain a positive MemTotal")
    free = min(total, values.get("MemFree", 0))
    cached = values.get("Cached", 0)
    buffers = values.get("Buffers", 0)
    reclaimable = values.get("SReclaimable", 0)
    shmem = values.get("Shmem", 0)
    fallback_available = free + buffers + cached + reclaimable - shmem
    available = values.get("MemAvailable", fallback_available)
    available = _clamp(available, free, total)
    return MemoryStats(
        total=total,
        available=available,
        free=free,
        buffers=buffers,
        cached=cached,
        reclaimable=reclaimable,
        shmem=shmem,
        active=values.get("Active", 0),
        inactive=values.get("Inactive", 0),
        anon=values.get("AnonPages", 0),
        slab=values.get("Slab", 0),
        page_tables=values.get("PageTables", 0),
        kernel_stack=values.get("KernelStack", 0),
        dirty=values.get("Dirty", 0),
        writeback=values.get("Writeback", 0),
        swap_total=values.get("SwapTotal", 0),
        swap_free=values.get("SwapFree", 0),
        huge_total=values.get("HugePages_Total", 0),
        huge_free=values.get("HugePages_Free", 0),
        huge_page_size=values.get("Hugepagesize", 0),
    )


def _pressure_line(text: str) -> PressureLine:
    values: dict[str, str] = {}
    for item in text.split()[1:]:
        key, separator, value = item.partition("=")
        if separator:
            values[key] = value
    try:
        return PressureLine(
            avg10=max(0.0, float(values.get("avg10", "0"))),
            avg60=max(0.0, float(values.get("avg60", "0"))),
            avg300=max(0.0, float(values.get("avg300", "0"))),
            total_us=max(0, int(values.get("total", "0"))),
        )
    except ValueError:
        return PressureLine()


def parse_pressure(text: str) -> MemoryPressure:
    parsed: dict[str, PressureLine] = {}
    for line in text.splitlines():
        name = line.split(maxsplit=1)[0] if line.strip() else ""
        if name in ("some", "full"):
            parsed[name] = _pressure_line(line)
    return MemoryPressure(
        some=parsed.get("some", PressureLine()),
        full=parsed.get("full", PressureLine()),
        supported=bool(parsed),
    )


def parse_vmstat(text: str) -> VmCounters:
    values: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            values[fields[0]] = max(0, int(fields[1]))
        except ValueError:
            continue

    def sum_prefix(prefix: str) -> int:
        return sum(value for key, value in values.items() if key.startswith(prefix))

    def reclaim_total(prefix: str) -> int:
        # Newer kernels expose the same reclaim work both by caller
        # (kswapd/direct) and by page type (anon/file). Summing every key with
        # the prefix therefore counts most pages twice. Prefer the caller
        # counters and use the page-type split only on kernels without them.
        caller_keys = (f"{prefix}_kswapd", f"{prefix}_direct")
        if any(key in values for key in caller_keys):
            return sum(values.get(key, 0) for key in caller_keys)
        return values.get(f"{prefix}_anon", 0) + values.get(
            f"{prefix}_file", 0
        )

    return VmCounters(
        page_faults=values.get("pgfault", 0),
        major_faults=values.get("pgmajfault", 0),
        swap_in=values.get("pswpin", 0),
        swap_out=values.get("pswpout", 0),
        page_scan=reclaim_total("pgscan"),
        page_steal=reclaim_total("pgsteal"),
        oom_kills=values.get("oom_kill", 0),
        alloc_stalls=sum_prefix("allocstall"),
        compact_stalls=values.get("compact_stall", 0),
    )


def _status_value(status: dict[str, str], name: str) -> int:
    fields = status.get(name, "").split()
    if not fields:
        return 0
    try:
        value = int(fields[0])
    except ValueError:
        return 0
    return max(0, value * (KIB if len(fields) > 1 and fields[1] == "kB" else 1))


def _parse_status(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key] = value.strip()
    return values


class LinuxMemoryBackend:
    """Collect one coherent-enough snapshot from a Linux procfs tree."""

    def __init__(self, root: Path = Path("/")) -> None:
        self.root = root
        self.proc = root / "proc"
        self.hostname = socket.gethostname()
        self._users: dict[int, str] = {}

    def _user(self, uid: int) -> str:
        if uid in self._users:
            return self._users[uid]
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            name = str(uid)
        self._users[uid] = name
        return name

    def _process(self, directory: Path) -> ProcessMemory | None:
        try:
            pid = int(directory.name)
        except ValueError:
            return None
        status = _parse_status(_read_text(directory / "status"))
        if not status:
            return None
        try:
            ppid = int(status.get("PPid", "0"))
        except ValueError:
            ppid = 0
        uid_fields = status.get("Uid", "0").split()
        try:
            uid = int(uid_fields[0])
        except (IndexError, ValueError):
            uid = 0
        try:
            threads = max(1, int(status.get("Threads", "1")))
        except ValueError:
            threads = 1
        raw_command = b""
        try:
            with (directory / "cmdline").open("rb") as command_file:
                # The table cannot display an enormous command line, and
                # bounded reads keep many large argv blocks from inflating the
                # monitor's own memory use.
                raw_command = command_file.read(4096)
        except OSError:
            pass
        command = raw_command.replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        ).strip()
        name = status.get("Name", str(pid)).replace("\t", " ")
        if not command:
            command = f"[{name}]"
        return ProcessMemory(
            pid=pid,
            ppid=ppid,
            uid=uid,
            user=self._user(uid),
            name=name,
            state=status.get("State", "?").split(maxsplit=1)[0],
            threads=threads,
            rss=_status_value(status, "VmRSS"),
            virtual=_status_value(status, "VmSize"),
            anon=_status_value(status, "RssAnon"),
            file=_status_value(status, "RssFile"),
            shared=_status_value(status, "RssShmem"),
            command=command,
        )

    def processes(self) -> tuple[ProcessMemory, ...]:
        try:
            directories = sorted(
                (entry for entry in self.proc.iterdir() if entry.name.isdigit()),
                key=lambda path: int(path.name),
            )
        except OSError:
            return ()
        rows = []
        for directory in directories:
            process = self._process(directory)
            if process is not None:
                rows.append(process)
        return tuple(rows)

    def sample(self) -> MemorySnapshot:
        memory = parse_meminfo(_read_text(self.proc / "meminfo"))
        pressure_text = _read_text(self.proc / "pressure" / "memory")
        pressure = parse_pressure(pressure_text)
        vm = parse_vmstat(_read_text(self.proc / "vmstat"))
        return MemorySnapshot(
            timestamp=datetime.now().astimezone(),
            monotonic=time.monotonic(),
            hostname=self.hostname,
            memory=memory,
            pressure=pressure,
            vm=vm,
            processes=self.processes(),
        )


class DemoMemoryBackend:
    """Deterministic-looking animated data for screenshots and development."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.hostname = "kilix-demo"

    def sample(self) -> MemorySnapshot:
        now = time.monotonic()
        phase = now - self.started
        total = 32 * GIB
        used_ratio = 0.56 + math.sin(phase / 8.0) * 0.12
        available = int(total * (1.0 - used_ratio))
        free = int(available * 0.28)
        cache = int(total * (0.20 + math.sin(phase / 13.0) * 0.025))
        buffers = int(total * 0.012)
        memory = MemoryStats(
            total=total,
            available=available,
            free=free,
            buffers=buffers,
            cached=cache,
            reclaimable=int(total * 0.025),
            shmem=int(total * 0.018),
            active=int(total * 0.48),
            inactive=int(total * 0.30),
            anon=int(total * 0.38),
            slab=int(total * 0.046),
            page_tables=310 * MIB,
            kernel_stack=92 * MIB,
            dirty=int((4 + 3 * abs(math.sin(phase))) * MIB),
            writeback=0,
            swap_total=8 * GIB,
            swap_free=int(8 * GIB * (0.88 + 0.03 * math.sin(phase / 10.0))),
        )
        pressure_value = max(0.0, 1.8 + 2.0 * math.sin(phase / 7.0))
        pressure = MemoryPressure(
            some=PressureLine(pressure_value, pressure_value * 0.7, 1.1, int(phase * 18000)),
            full=PressureLine(pressure_value * 0.11, 0.16, 0.08, int(phase * 1600)),
            supported=True,
        )
        vm = VmCounters(
            page_faults=int(phase * 4200),
            major_faults=int(phase * (1.5 + abs(math.sin(phase / 5.0)) * 4)),
            swap_in=int(phase * 1.2),
            swap_out=int(phase * 0.8),
            page_scan=int(phase * 18),
            page_steal=int(phase * 12),
            oom_kills=0,
            alloc_stalls=int(phase / 60),
            compact_stalls=int(phase / 45),
        )
        specs = (
            ("firefox", 4.7, 38, "user", "firefox --profile work"),
            ("python3", 2.8, 13, "user", "python3 train.py"),
            ("kilix", 1.35, 22, "user", "kilix"),
            ("code", 0.92, 19, "user", "code --unity-launch"),
            ("postgres", 0.73, 9, "postgres", "postgres: writer"),
            ("Xwayland", 0.48, 5, "user", "/usr/bin/Xwayland"),
            ("pipewire", 0.12, 3, "user", "/usr/bin/pipewire"),
            ("systemd", 0.08, 1, "root", "/sbin/init"),
            ("bash", 0.045, 1, "user", "bash"),
            ("ssh-agent", 0.018, 1, "user", "ssh-agent"),
        )
        processes = tuple(
            ProcessMemory(
                pid=1200 + index * 137,
                ppid=1 if index > 4 else 900,
                uid=1000 if user == "user" else 0,
                user=user,
                name=name,
                state="S",
                threads=threads,
                rss=int(gib * GIB * (0.96 + math.sin(phase / (6 + index)) * 0.04)),
                virtual=int(gib * GIB * 2.4),
                anon=int(gib * GIB * 0.72),
                file=int(gib * GIB * 0.22),
                shared=int(gib * GIB * 0.02),
                command=command,
            )
            for index, (name, gib, threads, user, command) in enumerate(specs)
        )
        return MemorySnapshot(
            timestamp=datetime.now().astimezone(),
            monotonic=now,
            hostname=self.hostname,
            memory=memory,
            pressure=pressure,
            vm=vm,
            processes=processes,
        )
