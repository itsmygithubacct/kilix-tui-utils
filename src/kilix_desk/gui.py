"""The graphical session: raw terminal, select loop, pixel frames.

The same shape as Kilix Temps' dashboard loop — cbreak mode, the alternate
screen, escape-sequence key parsing, and a damage-aware presenter flushed on
its own deadline — with one addition the desktop needs that a dashboard does
not: an in-place hand-off. Launching a tool means leaving raw mode, deleting
the placed image, lending the child the real terminal, and re-entering with a
full repaint when it exits.
"""
from __future__ import annotations

import fcntl
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
from typing import Sequence

from . import facts
from .desk import State, handle
from .graphics import DesktopRenderer, KittyDisplay, terminal_pixel_size

# Raw byte sequences to the curses key numbers `keys.py` speaks, so the
# graphical session and the curses session drive the identical `handle()`.
_SEQUENCES = {
    b"\x1b[A": 259, b"\x1bOA": 259,      # up
    b"\x1b[B": 258, b"\x1bOB": 258,      # down
    b"\x1b[C": 261, b"\x1bOC": 261,      # right
    b"\x1b[D": 260, b"\x1bOD": 260,      # left
    b"\x1b[5~": 339,                     # page up
    b"\x1b[6~": 338,                     # page down
    b"\x1b[H": 262, b"\x1b[F": 360,      # home / end
}


class TerminalSession:
    """cbreak + alternate screen, restored no matter how the loop ends."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._attributes: list[object] | None = None

    def enter(self) -> None:
        self._attributes = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        # 1002/1006: button events, SGR-encoded. 1016: pixel coordinates,
        # which is native for a desktop drawn in pixels; a terminal without it
        # keeps reporting cells and the click mapping handles either.
        sys.stdout.write(
            "\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H"
            "\x1b[?1002h\x1b[?1006h\x1b[?1016h\x1b]2;Kilix TUI\x07")
        sys.stdout.flush()

    def leave(self) -> None:
        try:
            if self._attributes is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._attributes)
        finally:
            sys.stdout.write(
                "\x1b[?1016l\x1b[?1006l\x1b[?1002l"
                "\x1b[0m\x1b[?25h\x1b[?1049l\x1b]2;\x07")
            sys.stdout.flush()

    def __enter__(self) -> "TerminalSession":
        self.enter()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.leave()


_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


class GraphicalDesktop:
    def __init__(self, state: State) -> None:
        self.state = state
        self.renderer = DesktopRenderer()
        self.session = TerminalSession()
        self.display: KittyDisplay | None = None
        self.running = True
        self.redraw = True
        self.clear_screen = True
        self._cells = (100, 30)
        self._render_px = (1000, 600)
        self._raw_px = (1000, 600)
        # The desktop lends the terminal out through the same runner the
        # curses session uses; only the suspend/resume differs.
        state.runner = self._suspended_run

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _signal_stop(self, *_args: object) -> None:
        self.running = False

    def _signal_resize(self, *_args: object) -> None:
        self.redraw = True
        self.clear_screen = True

    def _suspended_run(self, argv: Sequence[str]) -> int:
        """Verb two, graphically: give the child the real terminal."""
        if self.display is not None:
            try:
                self.display.hide()
            except Exception:
                pass
        self.session.leave()
        try:
            return subprocess.call(list(argv))
        except FileNotFoundError:
            return 127
        except OSError:
            return 126
        finally:
            self.session.enter()
            self.redraw = True
            self.clear_screen = True

    # ── the loop ─────────────────────────────────────────────────────────────

    def _draw(self) -> None:
        size = shutil.get_terminal_size((100, 30))
        pixels = terminal_pixel_size(sys.stdout.fileno(), size.columns,
                                     size.lines)
        self._cells = (size.columns, size.lines)
        self._render_px = pixels
        try:
            packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ,
                                 b"\0" * 8)
            _rows, _cols, raw_w, raw_h = struct.unpack("HHHH", packed)
            if raw_w and raw_h:
                self._raw_px = (raw_w, raw_h)
            else:
                self._raw_px = pixels
        except (OSError, struct.error):
            self._raw_px = pixels
        frame = self.renderer.render(self.state, size.columns, size.lines,
                                     pixels)
        if self.display is None:
            return
        if self.clear_screen:
            sys.stdout.write("\x1b[2J\x1b[H")
            self.display.invalidate()
        self.display.present(frame, force_full=self.clear_screen)
        sys.stdout.flush()
        self.redraw = False
        self.clear_screen = False

    # ── mouse ────────────────────────────────────────────────────────────────

    def _to_render(self, x: int, y: int) -> tuple[int, int]:
        """Map a report (pixels with 1016, cells without) onto the frame."""
        columns, rows = self._cells
        render_w, render_h = self._render_px
        raw_w, raw_h = self._raw_px
        if x > columns + 1 or y > rows + 1:
            return (int(x * render_w / max(1, raw_w)),
                    int(y * render_h / max(1, raw_h)))
        return (int((x - 0.5) * render_w / max(1, columns)),
                int((y - 0.5) * render_h / max(1, rows)))

    def _click(self, x: int, y: int) -> None:
        if self.state.confirm is not None:
            return                    # power confirmations stay on the keys
        px, py = self._to_render(x, y)
        for kind, index, (x1, y1, x2, y2) in reversed(self.renderer.hits):
            if not (x1 <= px <= x2 and y1 <= py <= y2):
                continue
            if kind == "section":
                self.running = (handle(ord("1") + index, self.state)
                                and self.running)
            elif kind == "entry":
                if (self.state.focus == "entries"
                        and index == self.state.selected):
                    self.running = handle(10, self.state) and self.running
                else:
                    self.state.focus = "entries"
                    self.state.selected = index
            self.redraw = True
            return

    def _mouse(self, button: int, x: int, y: int, press: bool) -> None:
        if button in (64, 65):        # wheel, reported as presses
            key = 259 if button == 64 else 258
            if self.state.entries():
                self.state.focus = "entries"
            self.running = handle(key, self.state) and self.running
            self.redraw = True
            return
        if press and button & 3 == 0 and not button & 32:
            self._click(x, y)

    def _handle_bytes(self, data: bytes) -> None:
        data = getattr(self, "_pending", b"") + data
        self._pending = b""
        while data and self.running:
            if data.startswith(b"\x1b[<") and not _MOUSE.match(data):
                # A mouse report split across reads: keep it for the rest.
                if len(data) < 24:
                    self._pending = data
                    return
            mouse = _MOUSE.match(data)
            if mouse:
                self._mouse(int(mouse.group(1)), int(mouse.group(2)),
                            int(mouse.group(3)), mouse.group(4) == b"M")
                data = data[mouse.end():]
                continue
            matched = False
            for sequence, key in _SEQUENCES.items():
                if data.startswith(sequence):
                    self.running = handle(key, self.state) and self.running
                    data = data[len(sequence):]
                    matched = True
                    break
            if matched:
                self.redraw = True
                continue
            byte = data[0]
            data = data[1:]
            if byte == 27 and data[:1] in (b"[", b"O"):
                # An unrecognised escape sequence: swallow it whole rather
                # than feeding its letters to the section hotkeys.
                while data and not data[:1].isalpha() and data[:1] != b"~":
                    data = data[1:]
                data = data[1:]
                continue
            key = 27 if byte == 27 else byte
            if key in (10, 13):
                key = 10
            self.running = handle(key, self.state) and self.running
            self.redraw = True

    def run(self) -> int:
        previous = {}
        for sig, handler in ((signal.SIGINT, self._signal_stop),
                             (signal.SIGTERM, self._signal_stop),
                             (signal.SIGWINCH, self._signal_resize)):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
        try:
            with self.session:
                self.display = KittyDisplay(sys.stdout, max_fps=12.0)
                minute = time.strftime("%H:%M")
                next_refresh = time.monotonic() + 30.0
                try:
                    while self.running:
                        if self.redraw:
                            self._draw()
                        now = time.monotonic()
                        timeout = max(0.05, min(1.0, next_refresh - now))
                        deadline = self.display.next_deadline
                        if deadline is not None:
                            timeout = min(timeout, max(0.0, deadline - now))
                        try:
                            readable, _, _ = select.select(
                                [sys.stdin], [], [], timeout)
                        except InterruptedError:
                            readable = []
                        if readable:
                            try:
                                data = os.read(sys.stdin.fileno(), 512)
                            except OSError:
                                data = b""
                            if data:
                                self._handle_bytes(data)
                        result = self.display.flush()
                        if getattr(result, "emitted", False):
                            sys.stdout.flush()
                        if time.monotonic() >= next_refresh:
                            self.state.status = facts.status_rows()
                            next_refresh = time.monotonic() + 30.0
                            self.redraw = True
                        elif time.strftime("%H:%M") != minute:
                            minute = time.strftime("%H:%M")
                            self.redraw = True
                finally:
                    if self.display is not None:
                        self.display.close()
                        self.display = None
            return 0
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def run(state: State) -> int:
    return GraphicalDesktop(state).run()
