"""kilix-system — static facts about this machine.

Describes the machine; it never changes it. Anything that changes the machine
belongs in the plebian-os control TUI, and keeping that line sharp is what stops
the two tools blurring into each other.
"""
from __future__ import annotations

import platform
import socket

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc  # noqa: E402


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


def render(surface, state) -> None:
    height, width = surface.getmaxyx()
    surface.addstr(0, 0, "Kilix System"[: width - 1])
    for index, (label, value) in enumerate(state):
        row = 2 + index
        if row >= height - 1:
            break
        surface.addstr(row, 0, f"{label:<16}{value}"[: width - 1])
    surface.addstr(height - 1, 0, "r refresh · q quit"[: width - 1])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
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
