"""kilix-disk — filesystem usage, and an interruptible directory scan.

The directory scan is the one thing here that can take minutes, so it runs
incrementally from the event loop and can be abandoned. A monitor that blocks
the pane it is drawn in would be worse than no monitor.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402


class State:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, int, int, int]] = []
        self.selected = 0
        self.scan_root = ""
        self.scan_results: list[tuple[str, int]] = []
        self.scan_done = False
        self.refresh()

    def refresh(self) -> None:
        self.rows = []
        for device, mountpoint, fstype in proc.mounts():
            total, used, free = proc.disk_usage(mountpoint)
            if total:
                self.rows.append((device, mountpoint, total, used, free))
        self.selected = min(self.selected, max(0, len(self.rows) - 1))

    def scan(self, root: str, budget: int = 4000) -> None:
        """Walk a bounded number of entries per call so the UI stays live."""
        self.scan_root = root
        sizes: dict[str, int] = {}
        seen = 0
        for entry in os.scandir(root) if os.path.isdir(root) else []:
            if seen > budget:
                break
            total = 0
            if entry.is_dir(follow_symlinks=False):
                for dirpath, _dirnames, filenames in os.walk(
                        entry.path, onerror=lambda _e: None):
                    for name in filenames:
                        try:
                            total += os.lstat(
                                os.path.join(dirpath, name)).st_size
                        except OSError:
                            pass
                        seen += 1
                    if seen > budget:
                        break
            else:
                try:
                    total = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    total = 0
                seen += 1
            sizes[entry.name] = total
        self.scan_results = sorted(
            sizes.items(), key=lambda item: item[1], reverse=True)[:20]
        self.scan_done = seen <= budget


def render(surface, state: State) -> None:
    scanning = f" · largest in {state.scan_root}" if state.scan_root else ""
    body = shell.draw(
        surface,
        title="Disk",
        sections=("Filesystems",),
        summary=f"{len(state.rows)} mounted filesystems{scanning}",
        footer="↑/↓ select · Enter scan · r refresh · q quit",
    )
    row = body.top
    for index, (device, mountpoint, total, used, _free) in enumerate(state.rows):
        if row >= body.bottom - (2 if state.scan_results else 0):
            break
        marker = "▶" if index == state.selected else " "
        fraction = used / total if total else 0
        shell.put(
            surface, row, body.left,
            f"{marker} {mountpoint:<22.22} "
            f"{proc.human_bytes(used):>8}/{proc.human_bytes(total):<8} "
            f"{fraction * 100:4.0f}% "
            f"{proc.bar(fraction, max(0, body.width - 56))}",
            shell.tango.attr("selected") if index == state.selected else 0,
        )
        row += 1
    if state.scan_results:
        row += 1
        label = "" if state.scan_done else " (partial)"
        shell.put(surface, row, body.left,
                  f"largest in {state.scan_root}{label}",
                  shell.tango.attr("title"))
        row += 1
        for name, size in state.scan_results:
            if row >= body.bottom:
                break
            shell.put(surface, row, body.left + 1,
                      f"{proc.human_bytes(size):>9}  {name}")
            row += 1


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
        step = keymap.direction(key)
        if step and s.rows:
            s.selected = (s.selected + step) % len(s.rows)
        elif key in keymap.SELECT and s.rows:
            s.scan(s.rows[s.selected][1])
        elif keymap.is_refresh(key):
            s.refresh()
        return True

    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
