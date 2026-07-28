"""kilix-file — a file manager for the session that has no desktop provider.

Navigation and inspection only: no delete, no move, no overwrite. On an OS whose
desktop is a terminal this is reachable from a menu, and a destructive operation
one keystroke from a menu is how people lose data. Opening a file hands it to
`kilix run` or $EDITOR rather than interpreting it here.
"""
from __future__ import annotations

import stat as stat_module
import subprocess
import time

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc  # noqa: E402


class State:
    def __init__(self, start: str | None = None) -> None:
        self.cwd = os.path.abspath(start or os.getcwd())
        self.selected = 0
        self.message = ""
        self.entries: list[dict[str, object]] = []
        self.refresh()

    def refresh(self) -> None:
        rows: list[dict[str, object]] = []
        if os.path.dirname(self.cwd) != self.cwd:
            rows.append({"name": "..", "dir": True, "size": 0, "mtime": 0,
                         "mode": 0})
        try:
            for entry in sorted(os.scandir(self.cwd),
                                key=lambda e: (not e.is_dir(), e.name.lower())):
                try:
                    info = entry.stat(follow_symlinks=False)
                    rows.append({
                        "name": entry.name,
                        "dir": entry.is_dir(follow_symlinks=False),
                        "size": info.st_size, "mtime": info.st_mtime,
                        "mode": info.st_mode,
                    })
                except OSError:
                    continue
        except OSError as error:
            self.message = str(error)
        self.entries = rows
        self.selected = min(self.selected, max(0, len(rows) - 1))

    def enter(self) -> None:
        if not self.entries:
            return
        item = self.entries[self.selected]
        target = os.path.normpath(os.path.join(self.cwd, str(item["name"])))
        if item["dir"]:
            self.cwd, self.selected, self.message = target, 0, ""
            self.refresh()
        else:
            self.open_file(target)

    def open_file(self, path: str) -> None:
        """Hand the file to the environment; never interpret it here."""
        opener = os.environ.get("EDITOR") or "kilix"
        args = [opener, "run", path] if opener == "kilix" else [opener, path]
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.message = f"opened {os.path.basename(path)}"
        except OSError as error:
            self.message = f"cannot open: {error}"


def render(surface, state: State) -> None:
    height, width = surface.getmaxyx()
    surface.addstr(0, 0, f"Kilix Files — {state.cwd}"[: width - 1])
    visible = max(1, height - 4)
    start = max(0, min(state.selected - visible // 2,
                       max(0, len(state.entries) - visible)))
    for index, item in enumerate(state.entries[start:start + visible]):
        row = 2 + index
        marker = ">" if start + index == state.selected else " "
        name = str(item["name"]) + ("/" if item["dir"] else "")
        size = "" if item["dir"] else proc.human_bytes(int(item["size"]))
        stamp = (time.strftime("%Y-%m-%d %H:%M",
                               time.localtime(float(item["mtime"])))
                 if item["mtime"] else "")
        perms = stat_module.filemode(int(item["mode"])) if item["mode"] else ""
        surface.addstr(row, 0,
                       f"{marker} {name:<34.34} {size:>9} {stamp:>16} "
                       f"{perms}"[: width - 1])
    if state.message:
        surface.addstr(height - 2, 0, state.message[: width - 1])
    surface.addstr(height - 1, 0,
                   "Enter open · Backspace up · r refresh · q quit"[: width - 1])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    start = next((a for a in argv if not a.startswith("-")), None)
    state = State(start)
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, state) + "\n")
        return 0

    def handle(key: int, s: State) -> bool:
        if keymap.is_quit(key):
            return False
        if step := keymap.direction(key):
            if s.entries:
                s.selected = max(0, min(len(s.entries) - 1, s.selected + step))
        elif key in keymap.SELECT:
            s.enter()
        elif key in (263, 127, 8):
            parent = os.path.dirname(s.cwd)
            if parent != s.cwd:
                s.cwd, s.selected = parent, 0
                s.refresh()
        elif keymap.is_refresh(key):
            s.refresh()
        return True

    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
