"""kilix-cpu — load, per-core use, frequency, and the heaviest processes."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc  # noqa: E402


class State:
    def __init__(self) -> None:
        self.previous = proc.cpu_sample()
        self.current = self.previous
        self.usage = 0.0
        self.cores: list[float] = []
        self.model = proc.cpu_model()

    def refresh(self) -> None:
        self.previous, self.current = self.current, proc.cpu_sample()
        self.usage = proc.usage_since(self.previous, self.current)
        self.cores = proc.per_core_usage(self.previous, self.current)


def render(surface, state: State) -> None:
    height, width = surface.getmaxyx()
    one, five, fifteen = proc.loadavg()
    surface.addstr(0, 0, "Kilix CPU"[: width - 1])
    surface.addstr(1, 0, state.model[: width - 1])
    surface.addstr(3, 0, f"total {state.usage:5.1f}%  "
                         f"{proc.bar(state.usage / 100, max(0, width - 24))}"[: width - 1])
    surface.addstr(4, 0, f"load  {one:.2f} {five:.2f} {fifteen:.2f}   "
                         f"up {proc.human_duration(proc.uptime_seconds())}"[: width - 1])
    row = 6
    speeds = proc.cpu_mhz()
    for index, value in enumerate(state.cores):
        if row >= height - 12:
            break
        mhz = f"{speeds[index]:7.0f}MHz" if index < len(speeds) else ""
        surface.addstr(row, 0, f"cpu{index:<3} {value:5.1f}% "
                               f"{proc.bar(value / 100, max(0, width - 30))} {mhz}"[: width - 1])
        row += 1
    row += 1
    if row < height - 2:
        surface.addstr(row, 0, "heaviest by CPU time"[: width - 1]); row += 1
        for item in proc.processes(limit=height - row - 2, key="cpu_time"):
            if row >= height - 1:
                break
            surface.addstr(row, 2, f"{item['pid']:>7}  "
                                   f"{proc.human_duration(item['cpu_time']):>9}  "
                                   f"{item['name']}"[: width - 3])
            row += 1
    surface.addstr(height - 1, 0, keymap.FOOTER[: width - 1])


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

    return app.run(render, state, handle=handle, tick_ms=1000)


if __name__ == "__main__":
    raise SystemExit(main())
