"""Interactive dashboard lifecycle and input handling."""

from __future__ import annotations

from dataclasses import dataclass
import os
import select
import shutil
import signal
import sys
import termios
import time
import tty
from typing import Any, Protocol

from .collect import MemorySnapshot
from .graphics import KittyDisplay, terminal_pixel_size
from .model import MemoryModel
from .render import FrameOptions


class Backend(Protocol):
    def sample(self) -> MemorySnapshot: ...


class TerminalSession:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._attributes: list[Any] | None = None

    def __enter__(self) -> "TerminalSession":
        self._attributes = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write(
            "\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H"
            "\x1b]2;Kilix Memory\x07"
        )
        sys.stdout.flush()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self._attributes is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._attributes)
        finally:
            sys.stdout.write(
                "\x1b[0m\x1b[?25h\x1b[?1049l\x1b]2;\x07"
            )
            sys.stdout.flush()


@dataclass(frozen=True, slots=True)
class AppConfig:
    interval: float = 1.0


class DashboardApp:
    SORT_MODES = ("rss", "pid", "name", "user")

    def __init__(
        self,
        backend: Backend,
        model: MemoryModel,
        renderer: Any,
        config: AppConfig,
    ) -> None:
        self.backend = backend
        self.model = model
        self.renderer = renderer
        self.config = config
        self.options = FrameOptions(interval=config.interval)
        self.running = True
        self.redraw = True
        self.force_full = True
        self._display: KittyDisplay | None = None

    def _stop(self, signum: int, frame: object) -> None:
        del signum, frame
        self.running = False

    def _resize(self, signum: int, frame: object) -> None:
        del signum, frame
        self.redraw = True
        self.force_full = True
        if self._display is not None:
            self._display.invalidate()

    def _install_signals(self) -> dict[int, object]:
        previous: dict[int, object] = {}
        for sig, handler in (
            (signal.SIGINT, self._stop),
            (signal.SIGTERM, self._stop),
            (signal.SIGWINCH, self._resize),
        ):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
        return previous

    @staticmethod
    def _restore_signals(previous: dict[int, object]) -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    def _sample(self) -> None:
        try:
            self.model.update(self.backend.sample())
        except (OSError, ValueError) as error:
            self.options.notice = f"sample error: {error}"
        else:
            self.options.notice = ""
        self.redraw = True

    def _draw(self) -> None:
        terminal = shutil.get_terminal_size((110, 34))
        if getattr(self.renderer, "is_graphical", False):
            if self._display is None:
                raise RuntimeError("graphical display is not initialized")
            pixels = terminal_pixel_size(
                sys.stdout.fileno(), terminal.columns, terminal.lines
            )
            frame = self.renderer.render(
                self.model,
                terminal.columns,
                terminal.lines,
                self.options,
                pixel_size=pixels,
            )
            self._display.present(frame, force_full=self.force_full)
        else:
            frame = self.renderer.render(
                self.model,
                terminal.columns,
                terminal.lines,
                self.options,
            )
            sys.stdout.write("\x1b[H" + frame + "\x1b[0m\x1b[J")
            sys.stdout.flush()
        self.redraw = False
        self.force_full = False

    def _cycle_sort(self) -> None:
        try:
            index = self.SORT_MODES.index(self.options.sort_mode)
        except ValueError:
            index = 0
        self.options.sort_mode = self.SORT_MODES[
            (index + 1) % len(self.SORT_MODES)
        ]
        self.options.scroll = 0

    def _scroll(self, amount: int) -> None:
        self.options.scroll = max(0, self.options.scroll + amount)

    def _handle_input(self, data: bytes) -> None:
        if not data:
            self.running = False
            return
        # Parse the escape sequences emitted by ordinary and application
        # cursor modes before handling individual printable keys.
        replacements = {
            b"\x1b[A": "up",
            b"\x1bOA": "up",
            b"\x1b[B": "down",
            b"\x1bOB": "down",
            b"\x1b[5~": "page_up",
            b"\x1b[6~": "page_down",
            b"\x1b[H": "home",
            b"\x1b[F": "end",
        }
        actions: list[str] = []
        remaining = data
        while remaining:
            matched = False
            for sequence, action in replacements.items():
                if remaining.startswith(sequence):
                    actions.append(action)
                    remaining = remaining[len(sequence) :]
                    matched = True
                    break
            if matched:
                continue
            byte = remaining[0]
            remaining = remaining[1:]
            if byte in (3, 27):
                actions.append("quit")
            elif chr(byte) in ("q", "Q"):
                actions.append("quit")
            elif chr(byte) == " ":
                actions.append("pause")
            elif chr(byte) in ("r", "R"):
                actions.append("reset")
            elif chr(byte) in ("s", "S"):
                actions.append("sort")
            elif chr(byte) in ("h", "H", "?"):
                actions.append("help")
            elif chr(byte) in ("j", "J"):
                actions.append("down")
            elif chr(byte) in ("k", "K"):
                actions.append("up")

        for action in actions:
            if action == "quit":
                self.running = False
            elif action == "pause":
                self.options.paused = not self.options.paused
            elif action == "reset":
                self.model.reset_history()
            elif action == "sort":
                self._cycle_sort()
            elif action == "help":
                self.options.help_visible = not self.options.help_visible
            elif action == "up":
                self._scroll(-1)
            elif action == "down":
                self._scroll(1)
            elif action == "page_up":
                self._scroll(-10)
            elif action == "page_down":
                self._scroll(10)
            elif action == "home":
                self.options.scroll = 0
            elif action == "end":
                self.options.scroll = 1_000_000
        self.redraw = True

    def run(self) -> int:
        previous = self._install_signals()
        self._sample()
        next_sample = time.monotonic() + self.config.interval
        try:
            with TerminalSession():
                if getattr(self.renderer, "is_graphical", False):
                    self._display = KittyDisplay(
                        sys.stdout,
                        max_fps=max(2.0, min(12.0, 1.0 / self.config.interval + 2)),
                    )
                while self.running:
                    now = time.monotonic()
                    if not self.options.paused and now >= next_sample:
                        self._sample()
                        next_sample = now + self.config.interval
                    if self.redraw:
                        self._draw()
                    if self._display is not None:
                        self._display.flush()

                    now = time.monotonic()
                    timeout = 0.25
                    if not self.options.paused:
                        timeout = min(timeout, max(0.0, next_sample - now))
                    if self._display is not None:
                        deadline = self._display.next_deadline
                        if deadline is not None:
                            timeout = min(timeout, max(0.0, deadline - now))
                    readable, _, _ = select.select(
                        [sys.stdin.fileno()], [], [], timeout
                    )
                    if readable:
                        self._handle_input(os.read(sys.stdin.fileno(), 64))
        finally:
            if self._display is not None:
                self._display.close()
                self._display = None
            self._restore_signals(previous)
        return 0
