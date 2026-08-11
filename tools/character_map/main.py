"""kilix-character-map — browse, search, inspect, and copy Unicode characters."""
from __future__ import annotations

import os
import sys
import unicodedata
from dataclasses import dataclass

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, clipboard, keys as keymap, shell  # noqa: E402


@dataclass(frozen=True)
class Character:
    value: str
    codepoint: int
    name: str

    @property
    def label(self) -> str:
        return f"U+{self.codepoint:04X}  {self.value}  {self.name}"


def catalog() -> tuple[Character, ...]:
    """Useful printable Unicode blocks without allocating all 1.1M points."""
    ranges = ((0x20, 0x300), (0x2000, 0x2C00), (0x1F300, 0x1F700))
    rows: list[Character] = []
    for start, stop in ranges:
        for point in range(start, stop):
            value = chr(point)
            name = unicodedata.name(value, "")
            category = unicodedata.category(value)
            if name and not category.startswith(("C", "Z")):
                rows.append(Character(value, point, name))
    return tuple(rows)


class State:
    def __init__(self, query: str = "") -> None:
        self.all = catalog()
        self.filter = shell.Filter()
        self.filter.text = query
        self.selected = 0
        self.message = ""

    def rows(self) -> list[Character]:
        needle = self.filter.text.strip().casefold()
        if not needle:
            return list(self.all)
        normalized = needle.removeprefix("u+")
        return [
            row for row in self.all
            if needle in row.name.casefold()
            or normalized == f"{row.codepoint:x}"
            or needle == row.value.casefold()
        ]


def render(surface, state: State) -> None:
    rows = state.rows()
    state.selected = max(0, min(state.selected, max(0, len(rows) - 1)))
    summary = state.filter.summary(len(rows))
    if not summary:
        summary = state.message or f"{len(rows)} printable characters"
    body = shell.draw(
        surface,
        title="Character Map",
        sections=("Unicode",),
        summary=summary,
        summary_role="accent" if state.message or state.filter.active() else "muted",
        footer=(state.filter.footer() if state.filter.typing
                else "↑↓ move · / search · Enter copy · q quit"),
    )
    visible = max(1, body.height)
    start = max(0, min(state.selected - visible // 2,
                       max(0, len(rows) - visible)))
    for offset, row in enumerate(rows[start:start + visible]):
        index = start + offset
        selected = index == state.selected
        marker = "▶" if selected else " "
        shell.put(
            surface,
            body.top + offset,
            body.left,
            f"{marker} {row.label}"[:body.width],
            shell.tango.attr("selected") if selected else 0,
        )


def handle(key: int, state: State) -> bool:
    if state.filter.typing:
        state.filter.handle(key)
        state.selected = 0
        return True
    if keymap.is_quit(key):
        return False
    if state.filter.handle(key):
        state.selected = 0
        return True
    rows = state.rows()
    if step := keymap.direction(key):
        state.selected = max(0, min(len(rows) - 1, state.selected + step))
    elif key in keymap.SELECT and rows:
        row = rows[state.selected]
        state.message = (
            f"copied {row.value} ({row.name})"
            if clipboard.copy(row.value)
            else f"{row.value} ({row.name}) — clipboard unavailable"
        )
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    query = ""
    if "--find" in argv:
        index = argv.index("--find")
        if index + 1 >= len(argv):
            print("kilix-character-map: --find needs text", file=sys.stderr)
            return 2
        query = argv[index + 1]
    state = State(query)
    if "--print" in argv:
        for row in state.rows():
            print(row.label)
        return 0
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as out:
            out.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
