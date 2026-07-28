"""The event loop every text tool runs on.

Three things every one of these tools needs and none should reimplement:
raw-mode setup and guaranteed teardown, a resize-safe redraw, and a headless
`--screenshot` path so a tool can be tested and documented without taking over
a terminal. Kilix 95 already proved the value of that last one — its whole test
suite renders offscreen.

A tool supplies a `render(surface, state)` and optionally a `handle(key, state)`;
everything else lives here.
"""
from __future__ import annotations

import curses
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import keys as keymap


class Surface(Protocol):
    """The subset of a curses window the tools are allowed to use."""

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None: ...
    def getmaxyx(self) -> tuple[int, int]: ...


@dataclass
class TextSurface:
    """A capture target used by `--screenshot` and by the tests.

    Rendering into this instead of a real terminal is what lets every tool be
    asserted on as plain text.
    """

    height: int = 24
    width: int = 80
    lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.lines:
            self.lines = [" " * self.width for _ in range(self.height)]

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        del attr
        if not (0 <= y < self.height):
            return
        row = self.lines[y]
        text = text[: max(0, self.width - x)]
        self.lines[y] = (row[:x] + text + row[x + len(text):])[: self.width]

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def __str__(self) -> str:
        return "\n".join(line.rstrip() for line in self.lines).rstrip("\n")


def render_to_text(
    render: Callable[[Any, Any], None],
    state: Any,
    *,
    height: int = 24,
    width: int = 80,
) -> str:
    """Render one frame headlessly and return it as text."""
    surface = TextSurface(height=height, width=width)
    render(surface, state)
    return str(surface)


def run(
    render: Callable[[Any, Any], None],
    state: Any,
    *,
    handle: Callable[[int, Any], bool] | None = None,
    tick_ms: int | None = None,
) -> int:
    """Run a tool interactively until it quits.

    `handle` returns False to exit. `tick_ms` makes the loop wake up on its own
    so a monitoring tool can refresh without input.
    """

    def _loop(stdscr: Any) -> int:
        curses.curs_set(0)
        stdscr.keypad(True)
        if tick_ms:
            stdscr.timeout(tick_ms)
        while True:
            stdscr.erase()
            render(stdscr, state)
            stdscr.refresh()
            key = stdscr.getch()
            if key == -1:            # timeout tick: redraw only
                continue
            if key == curses.KEY_RESIZE:
                continue
            if handle is not None:
                if not handle(key, state):
                    return 0
            elif keymap.is_quit(key):
                return 0

    if os.environ.get("KILIX_TUI_HEADLESS") == "1":
        print(render_to_text(render, state))
        return 0
    return curses.wrapper(_loop)


def screenshot_argv(argv: list[str]) -> str | None:
    """Return the path for `--screenshot PATH`, or None."""
    if "--screenshot" in argv:
        index = argv.index("--screenshot")
        if index + 1 < len(argv):
            return argv[index + 1]
    return None
