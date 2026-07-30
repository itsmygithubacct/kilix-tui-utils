"""Pixel memory dashboard rendered by soft-raster-py and shown by Kitty."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import importlib
import os
from pathlib import Path
import struct
import sys
import termios
from typing import Any, Mapping, TextIO

from .model import MemoryModel
from .render import FrameOptions, display_text, format_bytes, format_rate


Color = str
Box = tuple[int, int, int, int]

BG = "#07101c"
BG_ALT = "#0a1728"
PANEL = "#0e2034"
PANEL_ALT = "#112942"
BORDER = "#294763"
TEXT = "#e2eaf4"
WHITE = "#ffffff"
MUTED = "#8297ae"
DIM = "#4d6680"
BLUE = "#419ce0"
RED = "#ef4141"


class GraphicsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GraphicsFrame:
    rgb: bytes
    width: int
    height: int
    columns: int
    rows: int
    content_key: str = "kilix-memory-dashboard"


def kitty_graphics_likely(
    environment: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environment is None else environment
    if env.get("KITTY_WINDOW_ID") or env.get("KILIX_STREAM") == "1":
        return True
    signature = " ".join(
        (env.get("TERM", ""), env.get("TERM_PROGRAM", ""))
    ).lower()
    return any(name in signature for name in ("kitty", "ghostty", "wezterm"))


def _import_from_siblings(module_name: str, paths: tuple[Path, ...]) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as original:
        for path in paths:
            if not path.is_dir():
                continue
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
            try:
                return importlib.import_module(module_name)
            except ImportError:
                continue
        raise original


def _presenter_module() -> Any:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        return _import_from_siblings(
            "kitty_frame_presenter",
            (
                repository_root.parents[1]
                / "kilix-modules/kitty-frame-presenter/src",
                repository_root.parents[1]
                / "kilix/third_party/kitty-frame-presenter/src",
            ),
        )
    except ImportError as error:
        raise GraphicsUnavailable(
            "kitty-frame-presenter is not installed and no sibling checkout was found"
        ) from error


def _soft_raster_backend() -> tuple[Any, Any]:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        module = _import_from_siblings(
            "soft_raster",
            (
                repository_root.parents[1]
                / "kilix-modules/soft-raster-py/src",
                repository_root.parents[1]
                / "kilix/third_party/soft-raster-py/src",
            ),
        )
    except ImportError as error:
        raise GraphicsUnavailable(
            "soft-raster-py is not installed and no sibling checkout was found"
        ) from error
    error_type = getattr(module, "SoftRasterError", RuntimeError)
    try:
        return module, module.default_library()
    except error_type as original:
        if os.environ.get("SOFT_RASTER_LIBRARY"):
            raise GraphicsUnavailable(
                f"soft-raster is unavailable: {original}"
            ) from original
        names = (
            ("soft-raster.dll", "libsoft-raster.dll")
            if sys.platform == "win32"
            else ("libsoft-raster.dylib", "libsoft-raster.so")
            if sys.platform == "darwin"
            else ("libsoft-raster.so",)
        )
        for directory in (
            repository_root.parents[1] / "kilix-modules/soft-raster/build",
            repository_root.parents[1] / "kilix/third_party/soft-raster/build",
        ):
            for name in names:
                candidate = directory / name
                if not candidate.is_file():
                    continue
                try:
                    return module, module.SoftRasterLibrary(candidate)
                except error_type:
                    continue
        raise GraphicsUnavailable(
            f"soft-raster is unavailable: {original}"
        ) from original


def graphics_available() -> tuple[bool, str]:
    try:
        _soft_raster_backend()
        _presenter_module()
    except GraphicsUnavailable as error:
        return False, str(error)
    return True, ""


def terminal_pixel_size(
    fd: int,
    columns: int,
    rows: int,
    *,
    maximum: tuple[int, int] = (1600, 1000),
) -> tuple[int, int]:
    pixel_width = 0
    pixel_height = 0
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        ioctl_rows, ioctl_columns, pixel_width, pixel_height = struct.unpack(
            "HHHH", packed
        )
        if ioctl_columns and ioctl_rows:
            columns, rows = ioctl_columns, ioctl_rows
    except (OSError, ValueError, struct.error):
        pass
    if pixel_width < columns * 4 or pixel_height < rows * 8:
        pixel_width = columns * 10
        pixel_height = rows * 20
    pixel_width = max(160, pixel_width)
    pixel_height = max(120, pixel_height)
    scale = min(1.0, maximum[0] / pixel_width, maximum[1] / pixel_height)
    return max(160, int(pixel_width * scale)), max(120, int(pixel_height * scale))


class KittyDisplay:
    def __init__(
        self,
        terminal: TextIO,
        *,
        max_fps: float = 8.0,
        presenter_class: Any | None = None,
    ) -> None:
        module = _presenter_module()
        presenter_class = presenter_class or module.FramePresenter
        raw_id = os.environ.get("KITTY_WINDOW_ID", str(os.getpid()))
        identity = int(raw_id) if raw_id.isdigit() else os.getpid()
        self.image_id = 8101 + identity % 800
        self.terminal = terminal
        self.stream = os.environ.get("KILIX_STREAM") == "1"
        self.in_tmux = bool(os.environ.get("TMUX"))
        self._wrap_tmux = module.wrap_tmux_passthrough
        self.presenter = presenter_class(
            terminal,
            image_id=self.image_id,
            stream=self.stream,
            in_tmux=self.in_tmux,
            max_fps=max_fps,
            stream_warmup_seconds=0,
        )
        self.closed = False

    @property
    def next_deadline(self) -> float | None:
        return self.presenter.next_deadline

    def present(self, frame: GraphicsFrame, *, force_full: bool = False) -> Any:
        return self.presenter.present(
            frame.rgb,
            frame.width,
            frame.height,
            frame.columns,
            frame.rows,
            content_key=frame.content_key,
            force_full=force_full,
        )

    def invalidate(self) -> None:
        self.presenter.invalidate()

    def flush(self) -> Any:
        return self.presenter.flush()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        delete = f"\x1b_Ga=d,d=I,i={self.image_id},q=2\x1b\\"
        if self.stream and self.in_tmux:
            delete = self._wrap_tmux(delete)
        try:
            self.terminal.write(delete)
            self.terminal.flush()
        finally:
            self.presenter.close()


def _ascii(text: object) -> str:
    return (
        display_text(text)
        .replace("—", "-")
        .replace("–", "-")
        .replace("•", "|")
        .replace("↑", "in")
        .replace("↓", "out")
        .encode("ascii", errors="replace")
        .decode("ascii")
    )


class GraphicalRenderer:
    is_graphical = True

    def __init__(self) -> None:
        self._soft_raster, self._library = _soft_raster_backend()

    @staticmethod
    def _font_scale(width: int, height: int) -> int:
        return 2 if width >= 1250 and height >= 760 else 1

    @staticmethod
    def _text_width(text: str, scale: int = 1) -> int:
        return len(_ascii(text)) * 8 * scale

    def _fit(self, text: str, width: int, scale: int = 1) -> str:
        value = _ascii(text)
        capacity = max(0, width // (8 * scale))
        if len(value) <= capacity:
            return value
        if capacity <= 1:
            return value[:capacity]
        return value[: capacity - 1] + "~"

    def _text(
        self,
        canvas: Any,
        box: Box,
        text: str,
        color: Color = TEXT,
        *,
        scale: int = 1,
        align: str = "left",
        valign: str = "center",
        bold: bool = False,
    ) -> None:
        left, top, right, bottom = box
        value = self._fit(text, max(0, right - left), scale)
        width = self._text_width(value, scale)
        height = 16 * scale
        if align == "right":
            x = right - width
        elif align == "center":
            x = left + (right - left - width) // 2
        else:
            x = left
        if valign == "top":
            y = top
        elif valign == "bottom":
            y = bottom - height
        else:
            y = top + (bottom - top - height) // 2
        drawer = canvas.text_shadow if bold else canvas.text
        drawer(x, y, value, color, scale=scale)

    @staticmethod
    def _panel(canvas: Any, box: Box, *, color: Color = PANEL) -> None:
        left, top, right, bottom = box
        canvas.fill_rect(left + 2, top + 3, right - left, bottom - top, "#030912", 0.75)
        canvas.fill_rect(left, top, right - left, bottom - top, color)
        canvas.stroke_rect(left, top, right - left, bottom - top, 1, BORDER)

    @staticmethod
    def _bar(
        canvas: Any,
        box: Box,
        ratio: float,
        color: Color,
        *,
        background: Color = "#1b344d",
    ) -> None:
        left, top, right, bottom = box
        width = max(0, right - left)
        height = max(0, bottom - top)
        canvas.fill_rect(left, top, width, height, background)
        fill = max(0, min(width, int(width * max(0.0, min(1.0, ratio)))))
        if fill:
            canvas.fill_rect(left, top, fill, height, color)

    def _history_graph(
        self,
        canvas: Any,
        box: Box,
        values: list[float],
        *,
        maximum: float,
        color: Color,
        fill: Color | None = None,
    ) -> None:
        left, top, right, bottom = box
        width = right - left
        height = bottom - top
        if width < 4 or height < 4:
            return
        canvas.fill_rect(left, top, width, height, "#091827")
        for fraction in (0.25, 0.5, 0.75):
            y = top + int(height * fraction)
            canvas.line(left, y, right, y, 1, "#18334d", 0.8, 3, 5)
        if not values:
            return
        source = values[-max(2, width // 2) :]
        span = max(0.01, maximum)
        points: list[tuple[int, int]] = []
        for index, value in enumerate(source):
            x = left + index * (width - 1) // max(1, len(source) - 1)
            ratio = max(0.0, min(1.0, value / span))
            y = bottom - 2 - int(ratio * max(1, height - 4))
            points.append((x, y))
        if fill is not None and len(points) >= 2:
            for first, second in zip(points, points[1:]):
                canvas.fill_triangle(
                    first[0],
                    first[1],
                    second[0],
                    second[1],
                    second[0],
                    bottom,
                    fill,
                    0.35,
                )
                canvas.fill_triangle(
                    first[0],
                    first[1],
                    second[0],
                    bottom,
                    first[0],
                    bottom,
                    fill,
                    0.35,
                )
        for first, second in zip(points, points[1:]):
            canvas.line(*first, *second, 2, color)
        x, y = points[-1]
        canvas.fill_circle(x, y, 3, color)

    @staticmethod
    def _usage_color(percent: float) -> Color:
        if percent >= 90:
            return RED
        if percent >= 75:
            return RED
        return BLUE

    def _header(
        self,
        canvas: Any,
        box: Box,
        model: MemoryModel,
        options: FrameOptions,
        scale: int,
    ) -> None:
        snapshot = model.current
        assert snapshot is not None
        memory = snapshot.memory
        left, top, right, bottom = box
        canvas.fill_rect(left, top, right - left, bottom - top, BG_ALT)
        canvas.fill_rect(
            left, bottom - 3, right - left, 3,
            self._usage_color(memory.used_percent),
        )
        self._text(
            canvas,
            (left + 18, top + 3, right // 2, bottom - 2),
            "KILIX TUI",
            WHITE,
            scale=scale,
            bold=True,
        )
        mode = "PAUSED" if options.paused else f"LIVE {options.interval:.1f}s"
        status = (
            f"{snapshot.hostname}  |  {mode}  |  "
            f"{snapshot.timestamp.strftime('%H:%M:%S')}"
        )
        self._text(
            canvas,
            (right // 2, top + 3, right - 18, bottom - 2),
            status,
            MUTED,
            align="right",
        )

    def _summary_card(
        self,
        canvas: Any,
        box: Box,
        label: str,
        value: str,
        detail: str,
        ratio: float,
        color: Color,
        scale: int,
    ) -> None:
        self._panel(canvas, box)
        left, top, right, bottom = box
        self._text(
            canvas,
            (left + 12, top + 6, right - 12, top + 28),
            label,
            MUTED,
            bold=True,
        )
        value_scale = 2 if right - left >= 210 and bottom - top >= 88 else scale
        self._text(
            canvas,
            (left + 12, top + 24, right - 12, bottom - 28),
            value,
            color,
            scale=value_scale,
            bold=True,
        )
        self._text(
            canvas,
            (left + 12, bottom - 30, right - 12, bottom - 13),
            detail,
            TEXT,
            align="right",
        )
        self._bar(
            canvas,
            (left + 12, bottom - 10, right - 12, bottom - 5),
            ratio,
            color,
        )

    def _summary(
        self,
        canvas: Any,
        box: Box,
        model: MemoryModel,
        scale: int,
    ) -> None:
        snapshot = model.current
        assert snapshot is not None
        memory = snapshot.memory
        pressure = snapshot.pressure
        left, top, right, bottom = box
        gap = 10
        count = 4 if right - left >= 760 else 2
        rows = 1 if count == 4 else 2
        card_width = (right - left - gap * (count - 1)) // count
        card_height = (bottom - top - gap * (rows - 1)) // rows
        pressure_value = pressure.some.avg10 if pressure.supported else 0.0
        cards = (
            (
                "USED / AVAILABLE",
                f"{memory.used_percent:.1f}%",
                f"{format_bytes(memory.used)} / {format_bytes(memory.total)}",
                memory.used_percent / 100.0,
                self._usage_color(memory.used_percent),
            ),
            (
                "AVAILABLE",
                format_bytes(memory.available),
                f"{memory.available_percent:.1f}% ready",
                memory.available_percent / 100.0,
                BLUE,
            ),
            (
                "SWAP",
                (
                    format_bytes(memory.swap_used)
                    if memory.swap_total
                    else "NONE"
                ),
                (
                    f"{memory.swap_percent:.1f}% of {format_bytes(memory.swap_total)}"
                    if memory.swap_total
                    else "not configured"
                ),
                memory.swap_percent / 100.0,
                BLUE,
            ),
            (
                "PRESSURE / AVG10",
                f"{pressure_value:.2f}%" if pressure.supported else "N/A",
                (
                    f"full {pressure.full.avg10:.2f}%"
                    if pressure.supported
                    else "PSI unavailable"
                ),
                min(1.0, pressure_value / 25.0),
                RED if pressure_value >= 5 else BLUE,
            ),
        )
        for index, card in enumerate(cards):
            row = index // count
            column = index % count
            x = left + column * (card_width + gap)
            y = top + row * (card_height + gap)
            self._summary_card(
                canvas,
                (x, y, x + card_width, y + card_height),
                *card,
                scale,
            )

    def _composition(
        self,
        canvas: Any,
        box: Box,
        model: MemoryModel,
        scale: int,
    ) -> None:
        snapshot = model.current
        assert snapshot is not None
        memory = snapshot.memory
        self._panel(canvas, box)
        left, top, right, bottom = box
        self._text(
            canvas,
            (left + 12, top + 6, right - 12, top + 28),
            "PHYSICAL MEMORY COMPOSITION",
            MUTED,
            bold=True,
        )
        colors = {
            "apps": BLUE,
            "cache": BLUE,
            "buffers": WHITE,
            "free": "#24425d",
        }
        bar = (left + 14, top + 34, right - 14, top + 58)
        total_width = bar[2] - bar[0]
        x = bar[0]
        for name, value in memory.composition:
            width = (
                total_width - (x - bar[0])
                if name == "free"
                else int(total_width * value / memory.total)
            )
            if width > 0:
                canvas.fill_rect(x, bar[1], width, bar[3] - bar[1], colors[name])
            x += width
        canvas.stroke_rect(*(
            bar[0], bar[1], bar[2] - bar[0], bar[3] - bar[1]
        ), 1, BORDER)
        line_y = top + 65
        column_width = max(80, (right - left - 24) // 4)
        for index, (name, value) in enumerate(memory.composition):
            x = left + 12 + index * column_width
            canvas.fill_rect(x, line_y + 5, 7, 7, colors[name])
            self._text(
                canvas,
                (x + 12, line_y, min(right - 8, x + column_width), line_y + 18),
                f"{name.upper()} {format_bytes(value, short=True)}",
                TEXT,
                scale=scale,
            )
        details = (
            f"Anon {format_bytes(memory.anon, short=True)}   "
            f"Slab {format_bytes(memory.slab, short=True)}   "
            f"Tables {format_bytes(memory.page_tables, short=True)}   "
            f"Dirty {format_bytes(memory.dirty, short=True)}"
        )
        self._text(
            canvas,
            (left + 12, bottom - 25, right - 12, bottom - 6),
            details,
            MUTED,
            align="right",
        )

    def _activity(
        self,
        canvas: Any,
        box: Box,
        model: MemoryModel,
        scale: int,
    ) -> None:
        self._panel(canvas, box)
        left, top, right, bottom = box
        rates = model.rates
        self._text(
            canvas,
            (left + 12, top + 6, right - 12, top + 28),
            "PAGING ACTIVITY",
            MUTED,
            bold=True,
        )
        rows = (
            ("FAULTS", format_rate(rates.faults_per_second, unit="events"), BLUE),
            (
                "MAJOR",
                format_rate(rates.major_faults_per_second, unit="events"),
                RED if rates.major_faults_per_second else BLUE,
            ),
            ("SWAP IN", format_rate(rates.swap_in_bytes_per_second), BLUE),
            ("SWAP OUT", format_rate(rates.swap_out_bytes_per_second), BLUE),
            ("SCAN", format_rate(rates.scan_pages_per_second, unit="pages"), WHITE),
        )
        available = max(1, bottom - top - 40)
        row_height = max(17, available // len(rows))
        y = top + 30
        for label, value, color in rows:
            self._text(
                canvas,
                (left + 12, y, (left + right) // 2, y + row_height),
                label,
                MUTED,
                scale=scale,
            )
            self._text(
                canvas,
                ((left + right) // 2, y, right - 12, y + row_height),
                value,
                color,
                align="right",
                scale=scale,
                bold=True,
            )
            y += row_height

    def _history(
        self,
        canvas: Any,
        box: Box,
        model: MemoryModel,
        scale: int,
    ) -> None:
        snapshot = model.current
        assert snapshot is not None
        self._panel(canvas, box)
        left, top, right, bottom = box
        self._text(
            canvas,
            (left + 12, top + 5, right - 12, top + 27),
            "MEMORY HISTORY",
            MUTED,
            bold=True,
        )
        graph = (left + 12, top + 30, right - 12, bottom - 28)
        values = [point.used_percent for point in model.history]
        self._history_graph(
            canvas,
            graph,
            values,
            maximum=100.0,
            color=BLUE,
            fill="#163d67",
        )
        current = snapshot.memory.used_percent
        self._text(
            canvas,
            (left + 12, bottom - 25, right - 12, bottom - 5),
            f"USED {current:.1f}%   |   {len(values)} samples",
            TEXT,
            align="right",
            scale=scale,
        )

    def _processes(
        self,
        canvas: Any,
        box: Box,
        model: MemoryModel,
        options: FrameOptions,
        scale: int,
    ) -> None:
        snapshot = model.current
        assert snapshot is not None
        memory = snapshot.memory
        self._panel(canvas, box, color="#0d1d30")
        left, top, right, bottom = box
        rows = model.ordered_processes(options.sort_mode)
        header_height = 45
        row_height = max(20, 22 * scale)
        capacity = max(1, (bottom - top - header_height - 5) // row_height)
        options.scroll = max(0, min(options.scroll, max(0, len(rows) - capacity)))
        shown = rows[options.scroll : options.scroll + capacity]
        self._text(
            canvas,
            (left + 12, top + 4, right // 2, top + 25),
            f"PROCESSES BY {options.sort_mode.upper()}",
            MUTED,
            bold=True,
        )
        self._text(
            canvas,
            (right // 2, top + 4, right - 12, top + 25),
            (
                f"{options.scroll + 1 if shown else 0}-"
                f"{options.scroll + len(shown)} / {len(rows)}"
            ),
            MUTED,
            align="right",
        )
        width = right - left
        pid_x = left + 12
        user_x = left + max(72, int(width * 0.08))
        name_x = left + max(145, int(width * 0.20))
        rss_x = left + max(280, int(width * 0.48))
        pct_x = left + max(370, int(width * 0.60))
        command_x = left + max(440, int(width * 0.70))
        heading_y = top + 25
        for x, label in (
            (pid_x, "PID"),
            (user_x, "USER"),
            (name_x, "PROCESS"),
            (rss_x, "RSS"),
            (pct_x, "RAM%"),
            (command_x, "COMMAND"),
        ):
            if x < right - 30:
                self._text(
                    canvas,
                    (x, heading_y, right - 8, heading_y + 18),
                    label,
                    DIM,
                    valign="top",
                )
        y = top + header_height
        for index, process in enumerate(shown):
            if index % 2:
                canvas.fill_rect(
                    left + 3, y, right - left - 6, row_height, "#11253a"
                )
            row_bottom = y + row_height
            rss_color = (
                RED
                if process.rss >= 2 * 1024**3
                else BLUE
                if process.rss >= 1024**3
                else TEXT
            )
            values = (
                (pid_x, user_x - 5, str(process.pid), TEXT, "left"),
                (user_x, name_x - 5, process.user, MUTED, "left"),
                (name_x, rss_x - 5, process.name, TEXT, "left"),
                (
                    rss_x,
                    pct_x - 5,
                    format_bytes(process.rss, short=True),
                    rss_color,
                    "right",
                ),
                (
                    pct_x,
                    command_x - 5,
                    f"{process.rss_percent(memory.total):.1f}%",
                    rss_color,
                    "right",
                ),
                (command_x, right - 10, process.command, MUTED, "left"),
            )
            for x0, x1, value, color, align in values:
                if x0 >= right - 10 or x1 <= x0:
                    continue
                self._text(
                    canvas,
                    (x0, y, x1, row_bottom),
                    value,
                    color,
                    align=align,
                    scale=scale,
                )
            y += row_height

    def _footer(
        self,
        canvas: Any,
        box: Box,
        options: FrameOptions,
        scale: int,
    ) -> None:
        left, top, right, bottom = box
        text = options.notice or (
            f"q quit  |  space pause  |  r reset  |  "
            f"s sort [{options.sort_mode}]  |  arrows scroll  |  h help"
        )
        self._text(
            canvas,
            (left + 10, top, right - 10, bottom),
            text,
            MUTED,
            align="center",
            scale=scale,
        )

    def _help(
        self,
        canvas: Any,
        width: int,
        height: int,
        scale: int,
    ) -> None:
        canvas.fill_rect(0, 0, width, height, "#02060b", 0.78)
        panel_width = min(width - 30, 700)
        panel_height = min(height - 30, 390)
        left = (width - panel_width) // 2
        top = (height - panel_height) // 2
        box = (left, top, left + panel_width, top + panel_height)
        self._panel(canvas, box, color="#10243a")
        self._text(
            canvas,
            (left + 24, top + 16, box[2] - 24, top + 58),
            "KILIX TUI · MEMORY HELP",
            WHITE,
            scale=2 if panel_width > 500 else scale,
            bold=True,
        )
        entries = (
            ("q / Esc", "quit and restore the terminal"),
            ("Space", "pause or resume sampling"),
            ("r", "reset graph and rate history"),
            ("s", "cycle RSS / PID / name / user sorting"),
            ("arrows", "scroll the process table"),
            ("PgUp/PgDn", "scroll by one page"),
            ("h / ?", "close help"),
        )
        y = top + 75
        row_height = max(28, (panel_height - 135) // len(entries))
        for key, description in entries:
            self._text(
                canvas,
                (left + 28, y, left + 180, y + row_height),
                key,
                BLUE,
                bold=True,
            )
            self._text(
                canvas,
                (left + 190, y, box[2] - 24, y + row_height),
                description,
                TEXT,
            )
            y += row_height
        self._text(
            canvas,
            (left + 24, box[3] - 42, box[2] - 24, box[3] - 14),
            "Read-only monitoring: no kill, reclaim, renice, or tuning actions.",
            WHITE,
            align="center",
        )

    def _compact(
        self,
        canvas: Any,
        width: int,
        height: int,
        model: MemoryModel,
        options: FrameOptions,
        scale: int,
    ) -> None:
        snapshot = model.current
        assert snapshot is not None
        memory = snapshot.memory
        margin = 8
        self._header(canvas, (0, 0, width, 50), model, options, scale)
        panel = (margin, 58, width - margin, height - 34)
        self._panel(canvas, panel)
        color = self._usage_color(memory.used_percent)
        self._text(
            canvas,
            (panel[0] + 12, panel[1] + 8, panel[2] - 12, panel[1] + 50),
            f"RAM {memory.used_percent:.1f}%",
            color,
            scale=2 if width >= 390 else scale,
            bold=True,
        )
        self._text(
            canvas,
            (panel[0] + 12, panel[1] + 45, panel[2] - 12, panel[1] + 70),
            f"{format_bytes(memory.used)} used | {format_bytes(memory.available)} available",
            TEXT,
        )
        self._bar(
            canvas,
            (panel[0] + 12, panel[1] + 76, panel[2] - 12, panel[1] + 88),
            memory.used_percent / 100.0,
            color,
        )
        graph_top = panel[1] + 102
        graph_bottom = min(panel[3] - 70, graph_top + 90)
        self._history_graph(
            canvas,
            (panel[0] + 12, graph_top, panel[2] - 12, graph_bottom),
            [point.used_percent for point in model.history],
            maximum=100,
            color=BLUE,
            fill="#163d67",
        )
        rows = model.ordered_processes(options.sort_mode)[:3]
        y = graph_bottom + 8
        for process in rows:
            if y + 20 > panel[3] - 5:
                break
            self._text(
                canvas,
                (panel[0] + 12, y, panel[2] - 100, y + 20),
                f"{process.pid} {process.name}",
                TEXT,
            )
            self._text(
                canvas,
                (panel[2] - 95, y, panel[2] - 12, y + 20),
                format_bytes(process.rss, short=True),
                BLUE,
                align="right",
            )
            y += 21
        self._footer(canvas, (0, height - 30, width, height), options, scale)

    def render(
        self,
        model: MemoryModel,
        columns: int,
        rows: int,
        options: FrameOptions,
        *,
        pixel_size: tuple[int, int] | None = None,
    ) -> GraphicsFrame:
        width, height = pixel_size or (
            max(160, columns * 10),
            max(120, rows * 20),
        )
        canvas = self._soft_raster.Canvas(
            width, height, library=self._library
        )
        try:
            canvas.clear(BG)
            # Subtle horizontal depth bands without allocating an image layer.
            band_height = max(18, height // 20)
            for index, y in enumerate(range(0, height, band_height)):
                if index % 2:
                    canvas.fill_rect(0, y, width, band_height, "#091523", 0.35)
            scale = self._font_scale(width, height)
            if model.current is None:
                self._text(
                    canvas,
                    (10, 0, width - 10, height),
                    "KILIX TUI · MEMORY · WAITING FOR SAMPLE",
                    MUTED,
                    align="center",
                    scale=scale,
                )
            elif columns < 24 or rows < 8:
                self._text(
                    canvas,
                    (10, 0, width - 10, height // 2),
                    "KILIX MEMORY",
                    WHITE,
                    align="center",
                    valign="bottom",
                    scale=scale,
                    bold=True,
                )
                self._text(
                    canvas,
                    (10, height // 2 + 4, width - 10, height),
                    "pane needs 24 x 8 cells",
                    MUTED,
                    align="center",
                    valign="top",
                )
            elif width < 660 or height < 430 or rows < 20:
                self._compact(canvas, width, height, model, options, scale)
            else:
                margin = 12
                gap = 10
                header_height = 60
                footer_height = 30
                summary_height = 112 if width >= 760 else 206
                self._header(
                    canvas,
                    (0, 0, width, header_height),
                    model,
                    options,
                    scale,
                )
                summary_top = header_height + margin
                self._summary(
                    canvas,
                    (
                        margin,
                        summary_top,
                        width - margin,
                        summary_top + summary_height,
                    ),
                    model,
                    scale,
                )
                body_top = summary_top + summary_height + gap
                footer_top = height - footer_height
                process_height = max(150, int((footer_top - body_top) * 0.54))
                process_top = max(
                    body_top + 120,
                    footer_top - process_height,
                )
                upper_bottom = process_top - gap
                upper_width = width - margin * 2
                left_width = max(260, int(upper_width * 0.48))
                left_box = (
                    margin,
                    body_top,
                    margin + left_width,
                    upper_bottom,
                )
                right_left = left_box[2] + gap
                right_width = width - margin - right_left
                activity_width = max(170, int(right_width * 0.37))
                history_box = (
                    right_left,
                    body_top,
                    width - margin - activity_width - gap,
                    upper_bottom,
                )
                activity_box = (
                    history_box[2] + gap,
                    body_top,
                    width - margin,
                    upper_bottom,
                )
                self._composition(canvas, left_box, model, scale)
                self._history(canvas, history_box, model, scale)
                self._activity(canvas, activity_box, model, scale)
                self._processes(
                    canvas,
                    (margin, process_top, width - margin, footer_top - 3),
                    model,
                    options,
                    scale,
                )
                self._footer(
                    canvas,
                    (0, footer_top, width, height),
                    options,
                    scale,
                )
            if options.help_visible:
                self._help(canvas, width, height, scale)
            rgb = canvas.rgb_bytes()
        finally:
            canvas.close()
        return GraphicsFrame(
            rgb=rgb,
            width=width,
            height=height,
            columns=max(1, columns),
            rows=max(1, rows),
        )
