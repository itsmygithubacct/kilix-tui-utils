from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections import defaultdict
import math
import os
from pathlib import Path
import re
import time
from typing import Iterable


_NATURAL_PART = re.compile(r"(\d+)")


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in _NATURAL_PART.split(value)
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return None


def _read_float(path: Path) -> float | None:
    value = _read_text(path)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _read_temperature(path: Path, *, threshold: bool = False) -> float | None:
    raw = _read_float(path)
    if raw is None:
        return None
    value = raw / 1000.0 if abs(raw) > 1000.0 else raw
    minimum = 1.0 if threshold else 0.0
    if value <= minimum or value > 250.0:
        return None
    return value


def _safe_label(value: str, limit: int = 80) -> str:
    cleaned = "".join(character if character.isprintable() else "?" for character in value)
    return cleaned.strip()[:limit]


def _pretty_chip(name: str) -> str:
    name = _safe_label(name)
    lowered = name.lower()
    if lowered == "coretemp" or lowered == "x86_pkg_temp":
        return "CPU"
    if lowered == "acpitz":
        return "ACPI"
    if lowered.startswith("pch_"):
        return "PCH"
    if lowered.startswith("iwlwifi"):
        return "Wi-Fi"
    if lowered == "thinkpad":
        return "ThinkPad"
    if lowered == "nvme":
        return "NVMe"
    return name.replace("_", " ").strip().title() or "Sensor"


def _clean_label(label: str) -> str:
    label = _safe_label(label)
    label = re.sub(r"\s+", " ", label).strip()
    label = re.sub(r"Package id (\d+)", r"Package \1", label, flags=re.I)
    return label


@dataclass(frozen=True, slots=True)
class TemperatureSensor:
    key: str
    chip: str
    label: str
    source: str
    path: Path | None
    warning_hint: float | None = None
    critical_hint: float | None = None

    @property
    def display_name(self) -> str:
        return f"{self.chip} / {self.label}"


@dataclass(frozen=True, slots=True)
class FanSensor:
    key: str
    chip: str
    label: str
    source: str
    path: Path | None

    @property
    def display_name(self) -> str:
        return f"{self.chip} / {self.label}"


@dataclass(frozen=True, slots=True)
class ProcessLoad:
    name: str
    cpu_percent: float
    instances: int


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    cpu_percent: float | None
    load_1: float
    load_5: float
    load_15: float
    cpu_count: int
    uptime_seconds: float
    memory_percent: float | None
    top_processes: tuple[ProcessLoad, ...] = ()


@dataclass(frozen=True, slots=True)
class Sample:
    timestamp: datetime
    monotonic: float
    temperatures: dict[str, float]
    fans: dict[str, int]
    metrics: SystemMetrics


class ProcSampler:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._previous_cpu: tuple[int, int] | None = None
        self._process_previous: dict[int, tuple[str, int]] = {}
        self._process_scan_time: float | None = None
        self._process_cache: tuple[ProcessLoad, ...] = ()

    def _cpu_percent(self) -> float | None:
        text = _read_text(self.root / "proc/stat")
        if not text:
            return None
        first = text.splitlines()[0].split()
        if not first or first[0] != "cpu":
            return None
        try:
            values = [int(value) for value in first[1:]]
        except ValueError:
            return None
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        previous = self._previous_cpu
        self._previous_cpu = (idle, total)
        if previous is None:
            return None
        idle_delta = idle - previous[0]
        total_delta = total - previous[1]
        if total_delta <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta)))

    def _loads(self) -> tuple[float, float, float]:
        text = _read_text(self.root / "proc/loadavg")
        if not text:
            return (0.0, 0.0, 0.0)
        try:
            parts = text.split()
            return (float(parts[0]), float(parts[1]), float(parts[2]))
        except (IndexError, ValueError):
            return (0.0, 0.0, 0.0)

    def _uptime(self) -> float:
        text = _read_text(self.root / "proc/uptime")
        if not text:
            return 0.0
        try:
            return max(0.0, float(text.split()[0]))
        except (IndexError, ValueError):
            return 0.0

    def _memory_percent(self) -> float | None:
        text = _read_text(self.root / "proc/meminfo")
        if not text:
            return None
        values: dict[str, int] = {}
        for line in text.splitlines():
            key, separator, remainder = line.partition(":")
            if not separator:
                continue
            try:
                values[key] = int(remainder.strip().split()[0])
            except (IndexError, ValueError):
                continue
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (total - available) / total))

    def _top_processes(self, now: float) -> tuple[ProcessLoad, ...]:
        if self._process_scan_time is not None and now - self._process_scan_time < 2.0:
            return self._process_cache

        current: dict[int, tuple[str, int]] = {}
        proc_root = self.root / "proc"
        try:
            entries = list(proc_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            text = _read_text(entry / "stat")
            parsed = _parse_process_stat(text or "")
            if parsed is not None:
                current[int(entry.name)] = parsed

        previous_time = self._process_scan_time
        previous = self._process_previous
        self._process_previous = current
        self._process_scan_time = now
        if previous_time is None or now <= previous_time:
            self._process_cache = ()
            return self._process_cache

        elapsed = now - previous_time
        try:
            ticks_per_second = os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError):
            ticks_per_second = 100
        grouped_cpu: defaultdict[str, float] = defaultdict(float)
        grouped_instances: defaultdict[str, int] = defaultdict(int)
        for pid, (name, ticks) in current.items():
            old = previous.get(pid)
            if old is None or old[0] != name:
                continue
            delta = ticks - old[1]
            if delta <= 0:
                continue
            percent = 100.0 * delta / max(1.0, ticks_per_second * elapsed)
            if percent < 0.05:
                continue
            grouped_cpu[name] += percent
            grouped_instances[name] += 1
        loads = [
            ProcessLoad(name, grouped_cpu[name], grouped_instances[name])
            for name in grouped_cpu
        ]
        loads.sort(key=lambda process: (-process.cpu_percent, process.name.lower()))
        self._process_cache = tuple(loads[:5])
        return self._process_cache

    def sample(self) -> SystemMetrics:
        now = time.monotonic()
        loads = self._loads()
        return SystemMetrics(
            cpu_percent=self._cpu_percent(),
            load_1=loads[0],
            load_5=loads[1],
            load_15=loads[2],
            cpu_count=os.cpu_count() or 1,
            uptime_seconds=self._uptime(),
            memory_percent=self._memory_percent(),
            top_processes=self._top_processes(now),
        )


def _parse_process_stat(text: str) -> tuple[str, int] | None:
    """Return a process name and user+system CPU ticks from /proc/PID/stat."""

    left = text.find("(")
    right = text.rfind(")")
    if left < 0 or right <= left:
        return None
    name = _safe_label(text[left + 1 : right], limit=48) or "unknown"
    fields = text[right + 1 :].split()
    if len(fields) <= 12:
        return None
    try:
        return name, int(fields[11]) + int(fields[12])
    except ValueError:
        return None


class SensorBackend:
    """Read Linux thermal, hwmon and fan data directly from sysfs."""

    def __init__(self, root: str | Path = "/") -> None:
        self.root = Path(root)
        self._proc = ProcSampler(self.root)

    def discover_temperatures(self) -> list[TemperatureSensor]:
        sensors: list[TemperatureSensor] = []
        thermal_root = self.root / "sys/class/thermal"
        zones = sorted(
            thermal_root.glob("thermal_zone*"), key=lambda path: _natural_key(path.name)
        )
        for zone in zones:
            zone_type = _read_text(zone / "type") or zone.name
            chip = _pretty_chip(zone_type)
            warning: float | None = None
            critical: float | None = None
            for type_path in sorted(zone.glob("trip_point_*_type")):
                trip_type = (_read_text(type_path) or "").lower()
                temp_path = type_path.with_name(type_path.name.replace("_type", "_temp"))
                value = _read_temperature(temp_path, threshold=True)
                if value is None:
                    continue
                if trip_type == "critical":
                    critical = value if critical is None else min(critical, value)
                elif trip_type in {"hot", "active", "passive"}:
                    warning = value if warning is None else min(warning, value)
            index = zone.name.removeprefix("thermal_zone")
            sensors.append(
                TemperatureSensor(
                    key=f"zone:{index}:{zone_type}",
                    chip=chip,
                    label=f"zone {index}",
                    source="thermal-zone",
                    path=zone / "temp",
                    warning_hint=warning,
                    critical_hint=critical,
                )
            )

        hwmon_root = self.root / "sys/class/hwmon"
        hwmons = sorted(hwmon_root.glob("hwmon*"), key=lambda path: _natural_key(path.name))
        for hwmon in hwmons:
            raw_chip = _read_text(hwmon / "name") or hwmon.name
            chip = _pretty_chip(raw_chip)
            inputs = sorted(hwmon.glob("temp*_input"), key=lambda path: _natural_key(path.name))
            for input_path in inputs:
                prefix = input_path.name.removesuffix("_input")
                label = _read_text(hwmon / f"{prefix}_label") or prefix
                sensors.append(
                    TemperatureSensor(
                        key=f"hwmon:{hwmon.name}:{raw_chip}:{prefix}",
                        chip=chip,
                        label=_clean_label(label),
                        source=hwmon.name,
                        path=input_path,
                        warning_hint=_read_temperature(
                            hwmon / f"{prefix}_max", threshold=True
                        ),
                        critical_hint=_read_temperature(
                            hwmon / f"{prefix}_crit", threshold=True
                        ),
                    )
                )
        return sensors

    def discover_fans(self) -> list[FanSensor]:
        fans: list[FanSensor] = []
        hwmon_root = self.root / "sys/class/hwmon"
        hwmons = sorted(hwmon_root.glob("hwmon*"), key=lambda path: _natural_key(path.name))
        for hwmon in hwmons:
            raw_chip = _read_text(hwmon / "name") or hwmon.name
            chip = _pretty_chip(raw_chip)
            inputs = sorted(hwmon.glob("fan*_input"), key=lambda path: _natural_key(path.name))
            for input_path in inputs:
                prefix = input_path.name.removesuffix("_input")
                label = _read_text(hwmon / f"{prefix}_label") or prefix
                fans.append(
                    FanSensor(
                        key=f"fan:{hwmon.name}:{raw_chip}:{prefix}",
                        chip=chip,
                        label=_clean_label(label),
                        source=hwmon.name,
                        path=input_path,
                    )
                )
        return fans

    def discover(self) -> tuple[list[TemperatureSensor], list[FanSensor]]:
        return self.discover_temperatures(), self.discover_fans()

    def sample(
        self,
        temperatures: Iterable[TemperatureSensor],
        fans: Iterable[FanSensor],
    ) -> Sample:
        now = datetime.now().astimezone()
        monotonic = time.monotonic()
        temperature_values: dict[str, float] = {}
        for sensor in temperatures:
            if sensor.path is None:
                continue
            value = _read_temperature(sensor.path)
            if value is not None:
                temperature_values[sensor.key] = value
        fan_values: dict[str, int] = {}
        for fan in fans:
            if fan.path is None:
                continue
            value = _read_float(fan.path)
            if value is not None and 0.0 <= value <= 200_000.0:
                fan_values[fan.key] = int(round(value))
        return Sample(
            timestamp=now,
            monotonic=monotonic,
            temperatures=temperature_values,
            fans=fan_values,
            metrics=self._proc.sample(),
        )


class DemoBackend:
    """Synthetic backend for demos, screenshots and terminal testing."""

    def __init__(self) -> None:
        self._start = time.monotonic()
        self._proc = ProcSampler(Path("/"))
        self._temperatures = [
            TemperatureSensor("demo:acpi", "ACPI", "zone 0", "demo", None, None, 128.0),
            TemperatureSensor("demo:package", "CPU", "Package 0", "demo", None, 100.0, 100.0),
            TemperatureSensor("demo:gpu", "ThinkPad", "GPU", "demo", None, 85.0, 100.0),
            TemperatureSensor("demo:nvme", "NVMe", "Composite", "demo", None, 81.8, 84.8),
            TemperatureSensor("demo:wifi", "Wi-Fi", "radio", "demo", None, None, None),
        ]
        self._fans = [
            FanSensor("demo:fan1", "ThinkPad", "fan1", "demo", None),
            FanSensor("demo:fan2", "ThinkPad", "fan2", "demo", None),
        ]

    def discover(self) -> tuple[list[TemperatureSensor], list[FanSensor]]:
        return list(self._temperatures), list(self._fans)

    def sample(
        self,
        temperatures: Iterable[TemperatureSensor],
        fans: Iterable[FanSensor],
    ) -> Sample:
        del temperatures, fans
        monotonic = time.monotonic()
        phase = monotonic - self._start
        package = 82.0 + 11.5 * math.sin(phase / 7.0)
        values = {
            "demo:acpi": package + 1.8,
            "demo:package": package,
            "demo:gpu": 61.0 + 5.0 * math.sin(phase / 10.0 + 0.7),
            "demo:nvme": 48.0 + 2.5 * math.sin(phase / 13.0),
            "demo:wifi": 53.0 + 2.0 * math.sin(phase / 9.0),
        }
        fan_speed = int(3100 + max(0.0, package - 65.0) * 95)
        return Sample(
            timestamp=datetime.now().astimezone(),
            monotonic=monotonic,
            temperatures=values,
            fans={"demo:fan1": fan_speed, "demo:fan2": fan_speed + 180},
            metrics=self._proc.sample(),
        )
