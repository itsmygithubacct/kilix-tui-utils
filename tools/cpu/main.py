"""kilix-cpu — load, per-core use, frequency, and the heaviest processes."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402


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
    one, five, fifteen = proc.loadavg()
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
        f"up {proc.human_duration(proc.uptime_seconds())}",
        shell.tango.attr("muted"),
    )
    row = body.top + 3
    speeds = proc.cpu_mhz()
    for index, value in enumerate(state.cores):
        if row >= max(body.top, body.bottom - 10):
            break
        mhz = f"{speeds[index]:7.0f}MHz" if index < len(speeds) else ""
        shell.put(
            surface, row, body.left,
            f"cpu{index:<3} {value:5.1f}% "
            f"{proc.bar(value / 100, max(0, body.width - 30))} {mhz}",
        )
        row += 1
    row += 1
    if row < body.bottom:
        shell.put(surface, row, body.left, "heaviest by CPU time",
                  shell.tango.attr("title"))
        row += 1
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

    return app.run(render, state, handle=handle, tick_ms=1000)


if __name__ == "__main__":
    raise SystemExit(main())
