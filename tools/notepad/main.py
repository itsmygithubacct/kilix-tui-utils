"""kilix-notepad — a small UTF-8 editor that remains useful on a bare TTY."""
from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, shell  # noqa: E402

MAX_DOCUMENT_BYTES = 1024 * 1024
CTRL_Q = ord("q") - 96
CTRL_S = ord("s") - 96


class State:
    def __init__(self, path: str | None = None) -> None:
        self.path = os.path.abspath(os.path.expanduser(path)) if path else ""
        self.lines = [""]
        self.row = 0
        self.column = 0
        self.scroll = 0
        self.dirty = False
        self.message = ""
        self.naming = False
        self.name_entry = self.path
        self.quit_armed = False
        if self.path:
            self.load()

    def load(self) -> None:
        try:
            with open(self.path, "rb") as source:
                payload = source.read(MAX_DOCUMENT_BYTES + 1)
            if len(payload) > MAX_DOCUMENT_BYTES:
                raise ValueError(f"document exceeds {MAX_DOCUMENT_BYTES:,} bytes")
            text = payload.decode("utf-8")
        except (OSError, UnicodeError, ValueError) as error:
            self.message = f"cannot open: {error}"
            return
        self.lines = text.split("\n") or [""]
        self.message = f"opened {os.path.basename(self.path)}"

    def insert(self, value: str) -> None:
        line = self.lines[self.row]
        self.lines[self.row] = line[:self.column] + value + line[self.column:]
        self.column += len(value)
        self.changed()

    def newline(self) -> None:
        line = self.lines[self.row]
        self.lines[self.row:self.row + 1] = [line[:self.column], line[self.column:]]
        self.row += 1
        self.column = 0
        self.changed()

    def backspace(self) -> None:
        if self.column:
            line = self.lines[self.row]
            self.lines[self.row] = line[:self.column - 1] + line[self.column:]
            self.column -= 1
            self.changed()
        elif self.row:
            previous = self.lines[self.row - 1]
            self.column = len(previous)
            self.lines[self.row - 1:self.row + 1] = [previous + self.lines[self.row]]
            self.row -= 1
            self.changed()

    def changed(self) -> None:
        self.dirty = True
        self.quit_armed = False
        self.message = "modified"

    def move_vertical(self, step: int) -> None:
        self.row = max(0, min(len(self.lines) - 1, self.row + step))
        self.column = min(self.column, len(self.lines[self.row]))

    def save(self, path: str | None = None) -> bool:
        if path:
            self.path = os.path.abspath(os.path.expanduser(path))
        if not self.path:
            self.naming = True
            self.name_entry = ""
            self.message = "Save as:"
            return False
        directory = os.path.dirname(self.path) or os.getcwd()
        basename = os.path.basename(self.path)
        if not basename:
            self.message = "a file name is required"
            return False
        mode = 0o600
        try:
            info = os.stat(self.path)
            mode = stat.S_IMODE(info.st_mode)
        except FileNotFoundError:
            pass
        except OSError as error:
            self.message = f"cannot save: {error}"
            return False
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{basename}.", suffix=".tmp", dir=directory)
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as out:
                out.write("\n".join(self.lines))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
            temporary = ""
        except OSError as error:
            self.message = f"cannot save: {error}"
            return False
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self.dirty = False
        self.quit_armed = False
        self.message = f"saved {os.path.basename(self.path)}"
        return True


def render(surface, state: State) -> None:
    title = os.path.basename(state.path) if state.path else "Untitled"
    dirty = " *" if state.dirty else ""
    summary = (
        f"Save as: {state.name_entry}_"
        if state.naming else
        f"{state.message or state.path or 'new UTF-8 document'}{dirty}"
    )
    body = shell.draw(
        surface,
        title="Notepad",
        sections=(title,),
        summary=summary,
        summary_role="accent" if state.message or state.naming else "muted",
        footer=("type a path · Enter save · Esc cancel"
                if state.naming else
                "arrows move · Ctrl-S save · Ctrl-Q quit"),
        help_key=False,
    )
    visible = max(1, body.height)
    if state.row < state.scroll:
        state.scroll = state.row
    elif state.row >= state.scroll + visible:
        state.scroll = state.row - visible + 1
    number_width = max(3, len(str(len(state.lines))))
    for offset, line in enumerate(state.lines[state.scroll:state.scroll + visible]):
        index = state.scroll + offset
        selected = index == state.row
        prefix = f"{index + 1:>{number_width}} │ "
        shell.put(surface, body.top + offset, body.left, prefix,
                  shell.tango.attr("accent") if selected else shell.tango.attr("muted"))
        available = max(0, body.width - len(prefix))
        shown = line[:available]
        shell.put(surface, body.top + offset, body.left + len(prefix), shown)
        if selected and available:
            cursor_column = min(state.column, available - 1)
            cursor = line[state.column:state.column + 1] or " "
            shell.put(
                surface,
                body.top + offset,
                body.left + len(prefix) + cursor_column,
                cursor,
                shell.tango.attr("selected"),
            )


def handle(key: int, state: State) -> bool:
    if state.naming:
        if key == keymap.ESCAPE:
            state.naming = False
            state.message = "save cancelled"
        elif key in keymap.ENTER:
            state.naming = False
            state.save(state.name_entry)
        elif key in keymap.BACKSPACE:
            state.name_entry = state.name_entry[:-1]
        elif keymap.is_text(key):
            state.name_entry += chr(key)
        return True
    if key == CTRL_S:
        state.save()
    elif key == CTRL_Q:
        if state.dirty and not state.quit_armed:
            state.quit_armed = True
            state.message = "unsaved changes — Ctrl-Q again to discard"
            return True
        return False
    elif key in keymap.UP:
        state.move_vertical(-1)
    elif key in keymap.DOWN:
        state.move_vertical(1)
    elif key in keymap.LEFT:
        if state.column:
            state.column -= 1
        elif state.row:
            state.row -= 1
            state.column = len(state.lines[state.row])
    elif key in keymap.RIGHT:
        if state.column < len(state.lines[state.row]):
            state.column += 1
        elif state.row + 1 < len(state.lines):
            state.row += 1
            state.column = 0
    elif key in keymap.ENTER:
        state.newline()
    elif key in keymap.BACKSPACE:
        state.backspace()
    elif key == ord("\t"):
        state.insert("    ")
    elif keymap.is_text(key):
        state.insert(chr(key))
    return True


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Edit a UTF-8 text document.")
    result.add_argument("path", nargs="?", help="document to open")
    result.add_argument("--screenshot", help="write one text frame and exit")
    return result


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    state = State(options.path)
    if options.screenshot:
        with open(options.screenshot, "w", encoding="utf-8") as out:
            out.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle, help_key=False)


if __name__ == "__main__":
    raise SystemExit(main())
