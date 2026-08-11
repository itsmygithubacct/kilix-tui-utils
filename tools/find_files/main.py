"""kilix-find-files — bounded filename search with shared document opening."""
from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, openers, shell  # noqa: E402

MAX_RESULTS = 2000
MAX_DIRECTORIES = 200_000


@dataclass(frozen=True)
class Result:
    path: str
    directory: bool


class State:
    def __init__(self, root: str | None = None, query: str = "") -> None:
        self.root = os.path.abspath(os.path.expanduser(root or "~"))
        self.query = query
        self.results: list[Result] = []
        self.selected = 0
        self.editing = False
        self.message = "Press / to enter a name, extension, or glob."
        if query:
            self.search()

    def search(self) -> None:
        needle = self.query.strip()
        if not needle:
            self.results = []
            self.message = "Enter something to find."
            return
        folded = needle.casefold()
        use_glob = any(character in needle for character in "*?[")
        rows: list[Result] = []
        visited = 0
        try:
            walker = os.walk(self.root, followlinks=False)
            for directory, names, files in walker:
                visited += 1
                if visited > MAX_DIRECTORIES:
                    self.message = f"Stopped after {MAX_DIRECTORIES:,} directories."
                    break
                for name in (*names, *files):
                    matched = (
                        fnmatch.fnmatch(name.casefold(), folded)
                        if use_glob else folded in name.casefold()
                    )
                    if not matched:
                        continue
                    path = os.path.join(directory, name)
                    rows.append(Result(path, os.path.isdir(path)))
                    if len(rows) >= MAX_RESULTS:
                        self.message = f"Showing the first {MAX_RESULTS:,} matches."
                        break
                if len(rows) >= MAX_RESULTS:
                    break
        except OSError as error:
            self.message = str(error)
        else:
            if len(rows) < MAX_RESULTS and not self.message.startswith("Stopped"):
                self.message = f"{len(rows)} match" + ("" if len(rows) == 1 else "es")
        self.results = sorted(rows, key=lambda row: row.path.casefold())
        self.selected = 0

    def open_selected(self) -> None:
        if not self.results:
            return
        row = self.results[self.selected]
        if row.directory:
            self.root = row.path
            self.results = []
            self.query = ""
            self.message = "Directory selected. Press / to search inside it."
            return
        _opened, self.message = openers.open_document(row.path)


def render(surface, state: State) -> None:
    state.selected = max(0, min(state.selected, max(0, len(state.results) - 1)))
    summary = (
        f"find: {state.query}_  in {state.root}"
        if state.editing else f"{state.message} · {state.root}"
    )
    body = shell.draw(
        surface,
        title="Find Files",
        sections=("Search",),
        summary=summary,
        summary_role="accent" if state.editing else "muted",
        footer=("type a name · Enter search · Esc cancel"
                if state.editing else
                "↑↓ move · / new search · Enter open · q quit"),
        help_key=not state.editing,
    )
    visible = max(1, body.height)
    start = max(0, min(state.selected - visible // 2,
                       max(0, len(state.results) - visible)))
    for offset, row in enumerate(state.results[start:start + visible]):
        index = start + offset
        selected = index == state.selected
        marker = "▶" if selected else " "
        suffix = "/" if row.directory else ""
        try:
            label = os.path.relpath(row.path, state.root) + suffix
        except ValueError:
            label = row.path + suffix
        shell.put(
            surface,
            body.top + offset,
            body.left,
            f"{marker} {label}"[:body.width],
            shell.tango.attr("selected") if selected else 0,
        )


def handle(key: int, state: State) -> bool:
    if state.editing:
        if key == keymap.ESCAPE:
            state.editing = False
        elif key in keymap.ENTER:
            state.editing = False
            state.search()
        elif key in keymap.BACKSPACE:
            state.query = state.query[:-1]
        elif keymap.is_text(key):
            state.query += chr(key)
        return True
    if keymap.is_quit(key):
        return False
    if keymap.is_filter(key):
        state.query = ""
        state.editing = True
    elif step := keymap.direction(key):
        state.selected = max(
            0, min(len(state.results) - 1, state.selected + step))
    elif key in keymap.SELECT:
        state.open_selected()
    return True


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Find files by name without indexing.")
    result.add_argument("--root", default="~", help="directory to search")
    result.add_argument("--query", default="", help="name substring or glob")
    result.add_argument("--screenshot", help="write one text frame and exit")
    return result


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    state = State(options.root, options.query)
    if options.screenshot:
        with open(options.screenshot, "w", encoding="utf-8") as out:
            out.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
