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

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402


class State:
    def __init__(self, start: str | None = None) -> None:
        self.cwd = os.path.abspath(start or os.getcwd())
        self.selected = 0
        self.message = ""
        self.entries: list[dict[str, object]] = []
        self.filter = shell.Filter()
        self.refresh()

    def view(self) -> list[dict[str, object]]:
        """The rows on screen. Everything that reads a row goes through this,
        so the cursor can never index the unfiltered list."""
        return self.filter.apply(self.entries, key=lambda row: row["name"])

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
        rows = self.view()
        if not rows:
            return
        item = rows[min(self.selected, len(rows) - 1)]
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
    rows = state.view()
    state.selected = max(0, min(state.selected, max(0, len(rows) - 1)))
    body = shell.draw(
        surface,
        title="Files",
        sections=("Browse",),
        summary=state.filter.summary(len(rows)) or state.cwd,
        footer=(state.filter.footer() if state.filter.typing
                else "Enter open · Backspace up · / filter · r refresh · q quit"),
        summary_role="accent" if state.filter.active() else "muted",
    )
    visible = max(1, body.height - int(bool(state.message)))
    start = max(0, min(state.selected - visible // 2,
                       max(0, len(rows) - visible)))
    for index, item in enumerate(rows[start:start + visible]):
        row = body.top + index
        selected = start + index == state.selected
        marker = "▶" if selected else " "
        name = str(item["name"]) + ("/" if item["dir"] else "")
        size = "" if item["dir"] else proc.human_bytes(int(item["size"]))
        stamp = (time.strftime("%Y-%m-%d %H:%M",
                               time.localtime(float(item["mtime"])))
                 if item["mtime"] else "")
        perms = stat_module.filemode(int(item["mode"])) if item["mode"] else ""
        shell.put(
            surface, row, body.left,
            f"{marker} {name:<34.34} {size:>9} {stamp:>16} {perms}",
            shell.tango.attr("selected") if selected else 0,
        )
    if state.message:
        shell.put(surface, body.bottom - 1, body.left, state.message,
                  shell.tango.attr("accent"))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    start = next((a for a in argv if not a.startswith("-")), None)
    state = State(start)
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, state) + "\n")
        return 0

    def handle(key: int, s: State) -> bool:
        if s.filter.typing:
            s.filter.handle(key)
            return True
        if keymap.is_quit(key):
            return False
        if s.filter.handle(key):          # `/` opens it
            return True
        if step := keymap.direction(key):
            rows = s.view()
            if rows:
                s.selected = max(0, min(len(rows) - 1, s.selected + step))
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
