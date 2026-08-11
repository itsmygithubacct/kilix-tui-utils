"""kilix-cpu — load, per-core use, frequency, and the heaviest processes."""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell, telemetry  # noqa: E402, I001


class State:
    def __init__(self) -> None:
        self.previous = proc.cpu_sample()
        self.current = self.previous
        self.usage = 0.0
        self.cores: list[float] = []
        self.model = proc.cpu_model()
        self.loads = (0.0, 0.0, 0.0)
        self.uptime = 0.0
        self.speeds: list[float | None] = []
        self.shared_processes: tuple[Any, ...] | None = None

    def refresh(self) -> None:
        if snapshot := telemetry.snapshot():
            system = snapshot.system
            self.usage = system.cpu_percent or 0.0
            self.cores = [value or 0.0 for value in system.per_cpu_percent]
            self.loads = (system.load_1, system.load_5, system.load_15)
            self.uptime = system.uptime_seconds
            self.speeds = list(system.cpu_frequency_mhz)
            self.shared_processes = tuple(
                sorted(
                    (
                        process
                        for process in snapshot.processes
                        if process.cpu_cores >= 0.0005
                    ),
                    key=lambda process: (-process.cpu_cores, process.pid),
                )
            )
            return
        self.previous, self.current = self.current, proc.cpu_sample()
        self.usage = proc.usage_since(self.previous, self.current)
        self.cores = proc.per_core_usage(self.previous, self.current)
        self.loads = proc.loadavg()
        self.uptime = proc.uptime_seconds()
        self.speeds = [float(value) for value in proc.cpu_mhz()]
        self.shared_processes = None


def render(surface, state: State) -> None:
    one, five, fifteen = state.loads
    body = shell.draw(
        surface,
        title="CPU",
        sections=("Overview",),
        summary=state.model,
        footer=keymap.FOOTER,
    )
    shell.put(
        surface, body.top, body.left,
        f"total {state.usage:5.1f}%  "
        f"{proc.bar(state.usage / 100, max(0, body.width - 24))}",
        shell.tango.attr("accent"),
    )
    shell.put(
        surface, body.top + 1, body.left,
        f"load  {one:.2f} {five:.2f} {fifteen:.2f}   "
        f"up {proc.human_duration(state.uptime)}",
        shell.tango.attr("muted"),
    )
    row = body.top + 3
    speeds = state.speeds
    for index, value in enumerate(state.cores):
        if row >= max(body.top, body.bottom - 10):
            break
        speed = speeds[index] if index < len(speeds) else None
        mhz = f"{speed:7.0f}MHz" if speed is not None else ""
        shell.put(
            surface, row, body.left,
            f"cpu{index:<3} {value:5.1f}% "
            f"{proc.bar(value / 100, max(0, body.width - 30))} {mhz}",
        )
        row += 1
    row += 1
    if row < body.bottom:
        heading = (
            "heaviest now"
            if state.shared_processes is not None
            else "heaviest by CPU time"
        )
        shell.put(surface, row, body.left, heading, shell.tango.attr("title"))
        row += 1
        if state.shared_processes is not None:
            for process in state.shared_processes[:max(0, body.bottom - row)]:
                if row >= body.bottom:
                    break
                shell.put(
                    surface,
                    row,
                    body.left + 1,
                    f"{process.pid:>7}  {process.cpu_cores * 100:8.1f}%  "
                    f"{process.name}",
                )
                row += 1
        else:
            for item in proc.processes(
                    limit=max(0, body.bottom - row), key="cpu_time"):
                if row >= body.bottom:
                    break
                shell.put(
                    surface, row, body.left + 1,
                    f"{item['pid']:>7}  "
                    f"{proc.human_duration(item['cpu_time']):>9}  "
                    f"{item['name']}",
                )
                row += 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
        state.refresh()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, state) + "\n")
        return 0

    def handle(key: int, s: State) -> bool:
        if keymap.is_quit(key):
            return False
        s.refresh()
        return True

    state.refresh()
    return app.run(
        render,
        state,
        handle=handle,
        tick_ms=1000,
        on_tick=lambda current: current.refresh(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
