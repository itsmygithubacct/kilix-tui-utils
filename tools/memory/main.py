"""kilix-memory — RAM, swap, pressure, and the heaviest processes."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc  # noqa: E402


class State:
    def __init__(self) -> None:
        self.info: dict[str, int] = {}
        self.refresh()

    def refresh(self) -> None:
        self.info = proc.meminfo()


def render(surface, state: State) -> None:
    height, width = surface.getmaxyx()
    info = state.info
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = max(0, total - available)
    swap_total = info.get("SwapTotal", 0)
    swap_used = max(0, swap_total - info.get("SwapFree", 0))
    surface.addstr(0, 0, "Kilix Memory"[: width - 1])
    meter = max(0, width - 34)
    surface.addstr(2, 0, f"RAM  {proc.human_bytes(used):>8} / "
                         f"{proc.human_bytes(total):>8}  "
                         f"{proc.bar(used / total if total else 0, meter)}"[: width - 1])
    if swap_total:
        surface.addstr(3, 0, f"swap {proc.human_bytes(swap_used):>8} / "
                             f"{proc.human_bytes(swap_total):>8}  "
                             f"{proc.bar(swap_used / swap_total, meter)}"[: width - 1])
    else:
        surface.addstr(3, 0, "swap  (none configured)"[: width - 1])
    row = 5
    for label, key in (("cached", "Cached"), ("buffers", "Buffers"),
                       ("dirty", "Dirty"), ("shared", "Shmem")):
        if key in info and row < height - 3:
            surface.addstr(row, 0,
                           f"{label:<8} {proc.human_bytes(info[key]):>9}"[: width - 1])
            row += 1
    psi = proc.pressure("memory")
    if psi and row < height - 3:
        row += 1
        surface.addstr(row, 0, f"pressure some10s {psi.get('some_avg10', 0):.2f}  "
                               f"full10s {psi.get('full_avg10', 0):.2f}"[: width - 1])
        row += 1
    row += 1
    if row < height - 2:
        surface.addstr(row, 0, "heaviest by RSS"[: width - 1]); row += 1
        for item in proc.processes(limit=max(0, height - row - 2), key="rss"):
            if row >= height - 1:
                break
            surface.addstr(row, 2, f"{item['pid']:>7}  "
                                   f"{proc.human_bytes(item['rss']):>9}  "
                                   f"{item['name']}"[: width - 3])
            row += 1
    surface.addstr(height - 1, 0, keymap.FOOTER[: width - 1])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
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
