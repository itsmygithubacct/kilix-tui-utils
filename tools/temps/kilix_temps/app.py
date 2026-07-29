from __future__ import annotations

import csv
from dataclasses import dataclass
import os
from pathlib import Path
import select
import shutil
import signal
import sys
import termios
import time
import tty
from typing import Protocol, TextIO

from .graphics import KittyDisplay, terminal_pixel_size
from .model import Level, ThermalModel
from .render import FrameOptions, Renderer
from .sensors import FanSensor, Sample, TemperatureSensor
from .units import TemperatureUnit


class Backend(Protocol):
    def discover(self) -> tuple[list[TemperatureSensor], list[FanSensor]]: ...

    def sample(
        self,
        temperatures: list[TemperatureSensor],
        fans: list[FanSensor],
    ) -> Sample: ...


def default_log_path() -> Path:
    storage = os.environ.get("KILIX_TEMPS_STORAGE_HOME")
    if storage:
        root = Path(storage).expanduser()
    else:
        root = Path.home() / ".local/gpu_terminal/kilix-temps"
    return root / "state/temperatures.csv"


class CsvLogger:
    def __init__(self, path: Path, minimum_interval: float = 1.0) -> None:
        self.path = path
        self.minimum_interval = max(0.2, minimum_interval)
        self.active = False
        self._stream: TextIO | None = None
        self._writer: csv.writer | None = None
        self._last_write: float | None = None

    def start(self) -> None:
        if self.active:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        existed = self.path.exists() and self.path.stat().st_size > 0
        self._stream = self.path.open("a", encoding="utf-8", newline="")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._writer = csv.writer(self._stream)
        if not existed:
            self._writer.writerow(
                [
                    "timestamp",
                    "kind",
                    "key",
                    "chip",
                    "label",
                    "value",
                    "unit",
                    "level",
                    "warning",
                    "hot",
                    "limit",
                ]
            )
        self.active = True

    def stop(self) -> None:
        self.active = False
        self._writer = None
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        self._last_write = None

    def toggle(self) -> None:
        if self.active:
            self.stop()
        else:
            self.start()

    def write(
        self,
        sample: Sample,
        model: ThermalModel,
        fans: list[FanSensor],
    ) -> None:
        if not self.active or self._writer is None:
            return
        if (
            self._last_write is not None
            and sample.monotonic - self._last_write < self.minimum_interval
        ):
            return
        timestamp = sample.timestamp.isoformat(timespec="milliseconds")
        for state in model.active_states:
            if state.current is None:
                continue
            self._writer.writerow(
                [
                    timestamp,
                    "temperature",
                    state.sensor.key,
                    state.sensor.chip,
                    state.sensor.label,
                    f"{state.current:.3f}",
                    "celsius",
                    state.level.label,
                    f"{state.policy.warning:.3f}",
                    f"{state.policy.hot:.3f}",
                    f"{state.policy.critical:.3f}",
                ]
            )
        fan_by_key = {fan.key: fan for fan in fans}
        for key, rpm in sample.fans.items():
            fan = fan_by_key.get(key)
            self._writer.writerow(
                [
                    timestamp,
                    "fan",
                    key,
                    fan.chip if fan else "",
                    fan.label if fan else key,
                    str(rpm),
                    "rpm",
                    "",
                    "",
                    "",
                    "",
                ]
            )
        if self._stream is not None:
            self._stream.flush()
        self._last_write = sample.monotonic

    def close(self) -> None:
        self.stop()


class TerminalSession:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._attributes: list[object] | None = None

    def __enter__(self) -> "TerminalSession":
        self._attributes = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H\x1b]2;Kilix Temps\x07")
        sys.stdout.flush()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self._attributes is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._attributes)
        finally:
            sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l\x1b]2;\x07")
            sys.stdout.flush()


@dataclass(slots=True)
class AppConfig:
    interval: float
    no_bell: bool
    initial_log_path: Path
    start_logging: bool = False
    log_interval: float = 1.0
    temperature_unit: TemperatureUnit = TemperatureUnit.FAHRENHEIT


class DashboardApp:
    def __init__(
        self,
        backend: Backend,
        model: ThermalModel,
        renderer: Renderer,
        config: AppConfig,
    ) -> None:
        self.backend = backend
        self.model = model
        self.renderer = renderer
        self.config = config
        self.temperatures, self.fans = backend.discover()
        self.sample = backend.sample(self.temperatures, self.fans)
        initial_alerts = self.model.update(self.sample, self.temperatures)
        self._pending_bell = bool(initial_alerts)
        self.options = FrameOptions(
            interval=config.interval,
            temperature_unit=config.temperature_unit,
        )
        self.logger = CsvLogger(config.initial_log_path, config.log_interval)
        if config.start_logging:
            try:
                self.logger.start()
                self.options.logging = True
                self.options.log_path = str(self.logger.path)
                self.logger.write(self.sample, self.model, self.fans)
            except OSError as error:
                self.logger.stop()
                self.options.notice = f"LOG ERROR: {error.strerror or error}"
        self.running = True
        self.redraw = True
        self.clear_screen = True
        self._last_discovery = self.sample.monotonic
        self._display: KittyDisplay | None = None

    def _signal_stop(self, signum: int, frame: object) -> None:
        del signum, frame
        self.running = False

    def _signal_resize(self, signum: int, frame: object) -> None:
        del signum, frame
        self.redraw = True
        self.clear_screen = True

    def _install_signals(self) -> dict[int, object]:
        previous: dict[int, object] = {}
        for sig, handler in (
            (signal.SIGINT, self._signal_stop),
            (signal.SIGTERM, self._signal_stop),
            (signal.SIGWINCH, self._signal_resize),
        ):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
        return previous

    @staticmethod
    def _restore_signals(previous: dict[int, object]) -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    def _draw(self) -> None:
        size = shutil.get_terminal_size((100, 30))
        self.options.scroll = min(
            self.options.scroll, max(0, len(self.model.active_states) - 1)
        )
        self.options.logging = self.logger.active
        self.options.log_path = str(self.logger.path)
        if getattr(self.renderer, "is_graphical", False):
            if self._display is None:
                raise RuntimeError("graphical display was not initialized")
            pixels = terminal_pixel_size(
                sys.stdout.fileno(), size.columns, size.lines
            )
            frame = self.renderer.render(
                self.model,
                self.sample,
                self.fans,
                size.columns,
                size.lines,
                self.options,
                pixel_size=pixels,
            )
            if self.clear_screen:
                sys.stdout.write("\x1b[2J\x1b[H")
                self._display.invalidate()
            self._display.present(frame, force_full=self.clear_screen)
        else:
            frame = self.renderer.render(
                self.model,
                self.sample,
                self.fans,
                size.columns,
                size.lines,
                self.options,
            )
            prefix = "\x1b[2J\x1b[H" if self.clear_screen else "\x1b[H"
            sys.stdout.write(prefix + frame + "\x1b[0m")
        sys.stdout.flush()
        self.redraw = False
        self.clear_screen = False

    def _sample(self) -> None:
        now = time.monotonic()
        if now - self._last_discovery >= 30.0:
            self.temperatures, self.fans = self.backend.discover()
            self._last_discovery = now
        self.sample = self.backend.sample(self.temperatures, self.fans)
        alerts = self.model.update(self.sample, self.temperatures)
        try:
            self.logger.write(self.sample, self.model, self.fans)
        except OSError as error:
            self.logger.stop()
            self.options.notice = f"LOG ERROR: {error.strerror or error}"
        if alerts and not self.config.no_bell:
            sys.stdout.write("\x07")
        self.redraw = True

    def _key(self, key: str) -> None:
        if key in {"q", "Q", "escape"}:
            self.running = False
        elif key == " ":
            self.options.paused = not self.options.paused
        elif key in {"h", "H", "?"}:
            self.options.help_visible = not self.options.help_visible
        elif key in {"r", "R"}:
            self.model.reset_peaks()
        elif key in {"j", "J", "down"}:
            self.options.scroll = min(
                max(0, len(self.model.active_states) - 1), self.options.scroll + 1
            )
        elif key in {"k", "K", "up"}:
            self.options.scroll = max(0, self.options.scroll - 1)
        elif key in {"s", "S"}:
            self.options.sort_mode = "source" if self.options.sort_mode == "risk" else "risk"
            self.options.scroll = 0
        elif key in {"u", "U"}:
            self.options.temperature_unit = self.options.temperature_unit.toggled()
        elif key in {"l", "L"}:
            try:
                self.logger.toggle()
                self.options.notice = ""
            except OSError as error:
                self.logger.stop()
                self.options.notice = f"LOG ERROR: {error.strerror or error}"
        elif key in {"+", "="}:
            self.options.interval = max(0.2, self.options.interval / 1.5)
        elif key in {"-", "_"}:
            self.options.interval = min(60.0, self.options.interval * 1.5)
        elif key == "page_down":
            self.options.scroll = min(
                max(0, len(self.model.active_states) - 1), self.options.scroll + 8
            )
        elif key == "page_up":
            self.options.scroll = max(0, self.options.scroll - 8)
        self.redraw = True

    def _handle_input(self, data: bytes) -> None:
        sequences = {
            b"\x1b[A": "up",
            b"\x1b[B": "down",
            b"\x1b[5~": "page_up",
            b"\x1b[6~": "page_down",
        }
        while data:
            matched = False
            for sequence, name in sequences.items():
                if data.startswith(sequence):
                    self._key(name)
                    data = data[len(sequence) :]
                    matched = True
                    break
            if matched:
                continue
            byte = data[0]
            data = data[1:]
            if byte == 27:
                self._key("escape")
            else:
                self._key(bytes([byte]).decode("utf-8", errors="ignore"))

    def run(self) -> int:
        previous_signals = self._install_signals()
        try:
            with TerminalSession():
                if getattr(self.renderer, "is_graphical", False):
                    self._display = KittyDisplay(sys.stdout, max_fps=12.0)
                try:
                    if self._pending_bell and not self.config.no_bell:
                        sys.stdout.write("\x07")
                        sys.stdout.flush()
                    next_sample = time.monotonic() + self.options.interval
                    while self.running:
                        if self.redraw:
                            self._draw()
                        now = time.monotonic()
                        timeout = max(0.0, min(0.25, next_sample - now))
                        if self._display is not None:
                            deadline = self._display.next_deadline
                            if deadline is not None:
                                timeout = min(timeout, max(0.0, deadline - now))
                        readable, _, _ = select.select([sys.stdin], [], [], timeout)
                        if readable:
                            try:
                                data = os.read(sys.stdin.fileno(), 64)
                            except OSError:
                                data = b""
                            if data:
                                old_interval = self.options.interval
                                self._handle_input(data)
                                if old_interval != self.options.interval:
                                    next_sample = time.monotonic() + self.options.interval
                        if self._display is not None:
                            result = self._display.flush()
                            if result.emitted:
                                sys.stdout.flush()
                        now = time.monotonic()
                        if not self.options.paused and now >= next_sample:
                            self._sample()
                            next_sample = now + self.options.interval
                        elif self.options.paused:
                            next_sample = now + self.options.interval
                finally:
                    if self._display is not None:
                        self._display.close()
                        self._display = None
            return 0
        finally:
            self.logger.close()
            self._restore_signals(previous_signals)
