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
import sys
import time
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

    It also records the attribute each cell was written with. Plain text is
    still what `str()` returns and what every existing test compares, but a
    tool whose layout is made of coloured fills draws mostly spaces — without
    keeping the attributes there would be nothing headless to assert its shape
    against. See `attr_shape()`.
    """

    height: int = 24
    width: int = 80
    lines: list[str] = field(default_factory=list)
    attrs: list[list[int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.lines:
            self.lines = [" " * self.width for _ in range(self.height)]
        if not self.attrs:
            self.attrs = [[0] * self.width for _ in range(self.height)]

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if not (0 <= y < self.height):
            return
        row = self.lines[y]
        text = text[: max(0, self.width - x)]
        self.lines[y] = (row[:x] + text + row[x + len(text):])[: self.width]
        for offset in range(len(text)):
            column = x + offset
            if 0 <= column < self.width:
                self.attrs[y][column] = attr

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def attr_shape(self, legend: dict[int, str] | None = None) -> str:
        """The surface as one character per cell, keyed by attribute.

        Unstyled cells are spaces; every distinct attribute gets its own
        character, so a block, an elbow or a spine can be asserted on as a
        picture rather than as a list of coordinates.
        """
        seen: dict[int, str] = dict(legend or {})
        alphabet = "#=+*o.:~^%$&@!?"
        out = []
        for row in self.attrs:
            line = []
            for value in row:
                if not value:
                    line.append(" ")
                    continue
                if value not in seen:
                    seen[value] = alphabet[len(seen) % len(alphabet)]
                line.append(seen[value])
            out.append("".join(line).rstrip())
        return "\n".join(out).rstrip("\n")

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


def help_overlay(surface: Any, *, mouse: bool = False, extra: str = "") -> None:
    """Draw `?` help for whatever the last frame drew.

    The rows come from that tool's own key line, so a tool cannot advertise a
    key in its footer and omit it from its help — and no tool has to write its
    help twice.

    Only keys that are *actually true here* are added to that: `?` itself, and
    the mouse when this run enabled it. Appending the whole shared table looked
    generous and was dishonest — it promised "1 – 6 jump to a section" and "Tab
    next section" in single-section tools that do nothing with either, which is
    worse than saying nothing, because the user tries them.
    """
    from . import shell

    frame = shell.last_frame()
    rows = shell.help_rows_for(frame.get("footer", ""))
    if not any(keys.startswith("?") for keys, _text in rows):
        rows.append(("?", "show these keys"))
    if mouse:
        rows.append(("mouse", "click to select, click again to open, "
                              "wheel to scroll"))
    title = frame.get("title") or "Keys"
    shell.overlay(surface, title=f"{title} — keys", rows=rows,
                  note=extra or "any key closes this")


class IdleWatch:
    """Decides when an untouched loop has earned its idle action (F-SAVER).

    Kept apart from `run` so the arithmetic is testable with a fake clock:
    the loop only calls `touch()` on real input and asks `due()` on its
    timeout wake-ups. `after` is seconds; None, zero or negative means the
    watch never fires.
    """

    def __init__(self, after: float | None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.after = after if after and after > 0 else None
        self.clock = clock
        self.last = clock()

    def touch(self) -> None:
        self.last = self.clock()

    def due(self) -> bool:
        return (self.after is not None
                and self.clock() - self.last >= self.after)


def run(
    render: Callable[[Any, Any], None],
    state: Any,
    *,
    handle: Callable[[int, Any], bool] | None = None,
    tick_ms: int | None = None,
    on_tick: Callable[[Any], None] | None = None,
    mouse: bool = False,
    help_key: bool = True,
    idle_after: float | None = None,
    on_idle: Callable[[Any], None] | None = None,
) -> int:
    """Run a tool interactively until it quits.

    `handle` returns False to exit. `tick_ms` makes the loop wake up on its own;
    `on_tick` updates state before the redraw, so monitors do not repaint stale
    data. `mouse` reports clicks and the wheel as `curses.KEY_MOUSE` for
    `handle` to interpret via `getmouse`.

    `help_key` puts `?` on every tool from here, so seventeen tools do not each
    implement the same overlay. Tools that read typed text — a calculator, a
    filter box — set it False and keep `?` as an ordinary character.

    `idle_after` seconds without a key call `on_idle(state)` — the desktop
    starts a screensaver with it — and the clock starts over when it returns,
    which is the user coming back. The action may take the terminal; the loop
    repaints on the next pass either way.
    """
    showing_help = False
    started = False

    def _loop(stdscr: Any) -> int:
        nonlocal showing_help, started
        started = True
        try:
            curses.curs_set(0)
        except curses.error:
            # Some otherwise usable terminals cannot change cursor
            # visibility.  That cosmetic capability must not prevent the
            # event loop from opening or its wrapper from restoring the tty.
            pass
        stdscr.keypad(True)
        if mouse:
            try:
                curses.mousemask(curses.ALL_MOUSE_EVENTS)
            except Exception:
                pass
        idle = IdleWatch(idle_after if on_idle is not None else None)
        if tick_ms:
            stdscr.timeout(tick_ms)
        elif idle.after is not None:
            stdscr.timeout(1000)         # wake just often enough to check
        while True:
            stdscr.erase()
            render(stdscr, state)
            if showing_help:
                help_overlay(stdscr, mouse=mouse)
            stdscr.refresh()
            key = stdscr.getch()
            if key == -1:
                if idle.due():
                    on_idle(state)
                    idle.touch()         # returning input restarts the clock
                elif on_tick is not None:
                    on_tick(state)
                continue
            idle.touch()
            if key == curses.KEY_RESIZE:
                continue
            if showing_help:
                showing_help = False     # any key closes it, as the note says
                continue
            if help_key and keymap.is_help_char(key):
                showing_help = True
                continue
            if handle is not None:
                if not handle(key, state):
                    return 0
            elif keymap.is_quit(key):
                return 0

    if os.environ.get("KILIX_TUI_HEADLESS") == "1":
        # Size to the real terminal rather than the 24x80 the test helper
        # assumes: a headless run is how a tool gets looked at, and looking at
        # a wide layout squeezed into 80 columns misrepresents it.
        import shutil
        columns, rows = shutil.get_terminal_size(fallback=(80, 24))
        print(render_to_text(render, state, height=rows, width=columns))
        return 0
    try:
        return curses.wrapper(_loop)
    except curses.error:
        if started:
            raise            # a real failure inside the tool, not the setup
        # The screen never opened: no terminal on this side of a pipe or an
        # ssh command, or a TERM this machine has no terminfo entry for.
        # Say which, because the remedy differs.
        term = os.environ.get("TERM") or ""
        detail = (f"TERM={term} is not a terminal type this machine knows"
                  if term else "no TERM is set")
        print(f"this needs a terminal to draw on: {detail}", file=sys.stderr)
        return 1


def screenshot_argv(argv: list[str]) -> str | None:
    """Return the path for `--screenshot PATH`, or None."""
    if "--screenshot" in argv:
        index = argv.index("--screenshot")
        if index + 1 < len(argv):
            return argv[index + 1]
    return None
