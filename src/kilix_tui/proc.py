"""Readers for /proc and /sys, shared by the monitoring tools.

Kept in one place so `kilix-cpu`, `kilix-memory`, `kilix-disk`, and
`kilix-system` agree on what a number means. Every reader returns plain data
and never raises for a missing file: these paths differ across kernels and
containers, and a monitor that crashes because one sysfs node is absent is
worse than one that shows a blank field.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return default


def uptime_seconds() -> float:
    return float((_read("/proc/uptime", "0 0").split() or ["0"])[0])


def loadavg() -> tuple[float, float, float]:
    parts = _read("/proc/loadavg", "0 0 0").split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return 0.0, 0.0, 0.0


@dataclass
class CpuSample:
    """One /proc/stat reading. CPU use is a delta, so a single sample is
    meaningless — callers keep the previous one and call `usage_since`."""

    total: int
    idle: int
    per_core: list[tuple[int, int]]
    when: float


def cpu_sample() -> CpuSample:
    total = idle = 0
    per_core: list[tuple[int, int]] = []
    for line in _read("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            continue
        fields = line.split()
        try:
            values = [int(v) for v in fields[1:]]
        except ValueError:
            continue
        if not values:
            continue
        line_total = sum(values)
        line_idle = values[3] + (values[4] if len(values) > 4 else 0)
        if fields[0] == "cpu":
            total, idle = line_total, line_idle
        else:
            per_core.append((line_total, line_idle))
    return CpuSample(total, idle, per_core, time.monotonic())


def usage_since(previous: CpuSample, current: CpuSample) -> float:
    """Return aggregate CPU use in percent between two samples."""
    total_delta = current.total - previous.total
    idle_delta = current.idle - previous.idle
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))


def per_core_usage(previous: CpuSample, current: CpuSample) -> list[float]:
    result = []
    for (p_total, p_idle), (c_total, c_idle) in zip(
            previous.per_core, current.per_core):
        total_delta = c_total - p_total
        idle_delta = c_idle - p_idle
        if total_delta <= 0:
            result.append(0.0)
        else:
            result.append(
                max(0.0, min(100.0,
                             100.0 * (total_delta - idle_delta) / total_delta)))
    return result


def cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def cpu_mhz() -> list[float]:
    speeds = []
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("cpu MHz"):
            try:
                speeds.append(float(line.split(":", 1)[1]))
            except ValueError:
                pass
    return speeds


def meminfo() -> dict[str, int]:
    """Return /proc/meminfo in bytes."""
    values: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        try:
            amount = int(fields[0])
        except ValueError:
            continue
        values[key] = amount * 1024 if len(fields) > 1 else amount
    return values


def pressure(resource: str) -> dict[str, float]:
    """Return PSI averages for cpu/memory/io, empty when unsupported."""
    text = _read(f"/proc/pressure/{resource}")
    result: dict[str, float] = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        for field in fields[1:]:
            key, _, value = field.partition("=")
            try:
                result[f"{fields[0]}_{key}"] = float(value)
            except ValueError:
                pass
    return result


def thermal_zones() -> list[tuple[str, float]]:
    """Return (label, celsius) for every readable thermal zone."""
    zones: list[tuple[str, float]] = []
    base = "/sys/class/thermal"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return zones
    for name in names:
        if not name.startswith("thermal_zone"):
            continue
        raw = _read(f"{base}/{name}/temp").strip()
        if not raw:
            continue
        try:
            celsius = int(raw) / 1000.0
        except ValueError:
            continue
        label = _read(f"{base}/{name}/type").strip() or name
        zones.append((label, celsius))
    return zones


def mounts() -> list[tuple[str, str, str]]:
    """Return (device, mountpoint, fstype) for real filesystems only."""
    skip = {
        "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
        "securityfs", "pstore", "efivarfs", "bpf", "debugfs", "tracefs",
        "hugetlbfs", "mqueue", "fusectl", "configfs", "ramfs", "autofs",
        "binfmt_misc", "squashfs", "overlay", "nsfs",
    }
    result = []
    for line in _read("/proc/self/mounts").splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[2] in skip:
            continue
        result.append((fields[0], fields[1].replace("\\040", " "), fields[2]))
    return result


def disk_usage(path: str) -> tuple[int, int, int]:
    """Return (total, used, free) in bytes; zeros when unreadable."""
    try:
        usage = shutil.disk_usage(path)
        return usage.total, usage.used, usage.free
    except OSError:
        return 0, 0, 0


def processes(limit: int = 10, key: str = "rss") -> list[dict[str, object]]:
    """Return the heaviest processes by `rss` or `cpu_time`."""
    found: list[dict[str, object]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        stat_text = _read(f"/proc/{entry}/stat")
        if not stat_text:
            continue
        # comm can contain spaces and parentheses, so split around the last ')'
        close = stat_text.rfind(")")
        if close < 0:
            continue
        name = stat_text[stat_text.find("(") + 1:close]
        rest = stat_text[close + 2:].split()
        try:
            cpu_time = (int(rest[11]) + int(rest[12])) / os.sysconf(
                "SC_CLK_TCK")
            rss = int(rest[21]) * os.sysconf("SC_PAGE_SIZE")
        except (IndexError, ValueError, OSError):
            continue
        found.append({"pid": int(entry), "name": name, "rss": rss,
                      "cpu_time": cpu_time})
    found.sort(key=lambda item: item[key], reverse=True)  # type: ignore[arg-type]
    return found[:limit]


def human_bytes(value: float) -> str:
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(value) < 1024 or unit == "P":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}P"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def bar(fraction: float, width: int) -> str:
    """A proportional bar; the one place the tools agree on how a meter looks."""
    width = max(0, width)
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "█" * filled + "░" * (width - filled)
