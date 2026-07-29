"""The graphical session: raw terminal, select loop, pixel frames.

The same shape as Kilix Temps' dashboard loop — cbreak mode, the alternate
screen, escape-sequence key parsing, and a damage-aware presenter flushed on
its own deadline — with one addition the desktop needs that a dashboard does
not: an in-place hand-off. Launching a tool means leaving raw mode, deleting
the placed image, lending the child the real terminal, and re-entering with a
full repaint when it exits.
"""
from __future__ import annotations

import os
import select
import shutil
import signal
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
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H\x1b]2;Kilix TUI\x07")
        sys.stdout.flush()

    def leave(self) -> None:
        try:
            if self._attributes is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._attributes)
        finally:
            sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l\x1b]2;\x07")
            sys.stdout.flush()

    def __enter__(self) -> "TerminalSession":
        self.enter()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.leave()


class GraphicalDesktop:
    def __init__(self, state: State) -> None:
        self.state = state
        self.renderer = DesktopRenderer()
        self.session = TerminalSession()
        self.display: KittyDisplay | None = None
        self.running = True
        self.redraw = True
        self.clear_screen = True
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

    def _handle_bytes(self, data: bytes) -> None:
        while data and self.running:
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
                                data = os.read(sys.stdin.fileno(), 64)
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
