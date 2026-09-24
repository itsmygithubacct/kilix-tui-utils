"""kilix-system — static facts about this machine.

Describes the machine; it never changes it. Anything that changes the machine
belongs in the plebian-os control TUI, and keeping that line sharp is what stops
the two tools blurring into each other.
"""
from __future__ import annotations

import json
import platform
import socket
import time
from datetime import datetime, timezone

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if key:
                    values[key] = value.strip('"')
    except OSError:
        pass
    return values


def facts() -> list[tuple[str, str]]:
    release = _os_release()
    info = proc.meminfo()
    zones = proc.thermal_zones()
    hottest = max((c for _n, c in zones), default=None)
    rows = [
        ("host", socket.gethostname()),
        ("distro", release.get("PRETTY_NAME", "unknown")),
        ("kernel", f"{platform.system()} {platform.release()}"),
        ("arch", platform.machine()),
        ("cpu", proc.cpu_model()),
        ("cores", str(os.cpu_count() or 0)),
        ("memory", proc.human_bytes(info.get("MemTotal", 0))),
        ("swap", proc.human_bytes(info.get("SwapTotal", 0)) or "none"),
        ("uptime", proc.human_duration(proc.uptime_seconds())),
        ("python", platform.python_version()),
    ]
    if hottest is not None:
        rows.append(("hottest sensor", f"{hottest:.1f}°C"))
    root_total, root_used, _free = proc.disk_usage("/")
    if root_total:
        rows.append(("root fs",
                     f"{proc.human_bytes(root_used)} / "
                     f"{proc.human_bytes(root_total)}"))
    return rows


def health_snapshot(top_n: int = 10, sample_seconds: float = 0.1) -> dict:
    """Collect one dependency-free, machine-readable system health report."""
    previous = proc.cpu_sample()
    if sample_seconds > 0:
        time.sleep(sample_seconds)
    current = proc.cpu_sample()
    memory = proc.meminfo()
    total_memory = memory.get("MemTotal", 0)
    available_memory = memory.get("MemAvailable", memory.get("MemFree", 0))
    used_memory = max(0, total_memory - available_memory)
    swap_total = memory.get("SwapTotal", 0)
    swap_free = memory.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)

    disks = []
    for device, mountpoint, fstype in proc.mounts():
        total, used, free = proc.disk_usage(mountpoint)
        if total:
            disks.append({
                "mountpoint": mountpoint, "device": device, "fstype": fstype,
                "total": total, "used": used, "free": free,
                "percent": round(100.0 * used / total, 1),
            })

    processes = proc.processes(limit=top_n, key="cpu_time")
    for process in processes:
        rss = int(process["rss"])
        process["memory_percent"] = round(
            100.0 * rss / total_memory, 1) if total_memory else 0.0

    timestamp = time.time()
    loads = proc.loadavg()
    return {
        "schema_version": 1,
        "timestamp": timestamp,
        "timestamp_iso": datetime.fromtimestamp(
            timestamp, timezone.utc).isoformat(),
        "cpu": {
            "percent_per_core": proc.per_core_usage(previous, current),
            "percent_total": proc.usage_since(previous, current),
            "load_avg": list(loads),
            "core_count_logical": os.cpu_count() or 0,
        },
        "memory": {
            "total": total_memory,
            "available": available_memory,
            "used": used_memory,
            "percent": round(100.0 * used_memory / total_memory, 1)
            if total_memory else 0.0,
            "swap_total": swap_total,
            "swap_used": swap_used,
            "swap_percent": round(100.0 * swap_used / swap_total, 1)
            if swap_total else 0.0,
        },
        "disks": disks,
        "network": proc.network_io(),
        "top_processes": processes,
    }


def render(surface, state) -> None:
    host = next((value for label, value in state if label == "host"), "")
    body = shell.draw(
        surface,
        title="System",
        sections=("Facts",),
        summary=host,
        footer="r refresh · q quit",
    )
    for index, (label, value) in enumerate(state):
        row = body.top + index
        if row >= body.bottom:
            break
        shell.put(surface, row, body.left, f"{label:<16}{value}",
                  shell.tango.attr("muted") if index % 2 else 0)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv or "-j" in argv:
        top_n = 10
        if "--top" in argv:
            index = argv.index("--top")
            try:
                top_n = int(argv[index + 1])
            except (IndexError, ValueError):
                print("kilix-system: --top requires an integer", file=sys.stderr)
                return 2
            if not 1 <= top_n <= 50:
                print("kilix-system: --top must be between 1 and 50",
                      file=sys.stderr)
                return 2
        print(json.dumps(health_snapshot(top_n), indent=2, sort_keys=True))
        return 0
    state = facts()
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, state) + "\n")
        return 0
    if argv and argv[0] in ("--print", "-p"):
        for label, value in state:
            print(f"{label:<16}{value}")
        return 0

    def handle(key: int, s) -> bool:
        if keymap.is_quit(key):
            return False
        if keymap.is_refresh(key):
            s[:] = facts()
        return True

    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
