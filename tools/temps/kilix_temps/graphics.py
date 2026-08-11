"""Pixel dashboard and Kitty graphics transport for Kilix Temps.

The renderer deliberately knows nothing about terminal modes or input.  It
produces complete RGB frames; :class:`KittyDisplay` hands those frames to the
same damage-aware presenter used by Kilix's graphical applications.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import importlib
import os
from pathlib import Path
import socket
import struct
import sys
import termios
from typing import Any, Mapping, TextIO

from .model import Level, SensorState, ThermalModel
from .render import FrameOptions, format_duration
from .sensors import FanSensor, Sample


Color = tuple[int, int, int]
Box = tuple[int, int, int, int]

BG = (7, 13, 24)
BG_TOP = (10, 21, 37)
PANEL = (14, 25, 42)
PANEL_RAISED = (18, 32, 52)
PANEL_SOFT = (20, 36, 57)
BORDER = (37, 57, 82)
BORDER_BRIGHT = (57, 84, 116)
TEXT = (224, 232, 242)
MUTED = (132, 150, 171)
DIM = (83, 103, 128)
BLUE = (65, 156, 224)
RED = (239, 65, 65)
WHITE = (247, 250, 252)

LEVEL_COLORS: dict[Level, Color] = {
    Level.NORMAL: BLUE,
    Level.WARM: BORDER_BRIGHT,
    Level.HOT: RED,
    Level.CRITICAL: RED,
}


class GraphicsUnavailable(RuntimeError):
    """Raised when the optional graphical path cannot be used."""


@dataclass(frozen=True, slots=True)
class _RasterFont:
    """Small descriptor for soft-raster's embedded 8x16 bitmap font."""

    scale: int
    bold: bool = False


_TEXT_TRANSLATION = str.maketrans(
    {
        "\N{DEGREE SIGN}": "`",
        "\N{BULLET}": "|",
        "\N{HORIZONTAL ELLIPSIS}": ".",
        "\N{MINUS SIGN}": "-",
        "\N{EN DASH}": "-",
        "\N{EM DASH}": "-",
        "\N{MULTIPLICATION SIGN}": "x",
        "\N{UPWARDS ARROW}": "^",
        "\N{DOWNWARDS ARROW}": "v",
    }
)


def _raster_text(value: str) -> str:
    """Map dashboard typography onto the rasterizer's printable ASCII font."""

    return value.translate(_TEXT_TRANSLATION).encode("ascii", "replace").decode("ascii")


class _RasterDraw:
    """Layout-oriented drawing facade backed by a soft-raster canvas.

    Keeping the tiny facade local lets the layout code stay about boxes and
    metrics while every pixel primitive is provided by ``soft-raster-py``.
    """

    def __init__(self, canvas: Any) -> None:
        self.canvas = canvas

    @staticmethod
    def _box(box: Box) -> tuple[float, float, float, float]:
        left, top, right, bottom = box
        return float(left), float(top), float(right), float(bottom)

    def rectangle(
        self,
        box: Box,
        *,
        fill: Color | None = None,
        outline: Color | None = None,
        width: int = 1,
        alpha: float = 1.0,
    ) -> None:
        left, top, right, bottom = self._box(box)
        box_width = max(0.0, right - left)
        box_height = max(0.0, bottom - top)
        if fill is not None and box_width > 0 and box_height > 0:
            self.canvas.fill_rect(left, top, box_width, box_height, fill, alpha)
        if outline is not None and box_width > 0 and box_height > 0:
            self.canvas.stroke_rect(
                left, top, box_width, box_height, max(1, width), outline, alpha
            )

    def _rounded_fill(
        self,
        box: Box,
        radius: int,
        fill: Color,
        alpha: float = 1.0,
    ) -> None:
        left, top, right, bottom = self._box(box)
        box_width = max(0.0, right - left)
        box_height = max(0.0, bottom - top)
        radius = max(0, min(int(radius), int(box_width / 2), int(box_height / 2)))
        if box_width <= 0 or box_height <= 0:
            return
        if radius <= 1:
            self.canvas.fill_rect(left, top, box_width, box_height, fill, alpha)
            return
        self.canvas.fill_rect(
            left + radius,
            top,
            max(0.0, box_width - radius * 2),
            box_height,
            fill,
            alpha,
        )
        self.canvas.fill_rect(
            left,
            top + radius,
            box_width,
            max(0.0, box_height - radius * 2),
            fill,
            alpha,
        )
        for center_x, center_y in (
            (left + radius, top + radius),
            (right - radius, top + radius),
            (left + radius, bottom - radius),
            (right - radius, bottom - radius),
        ):
            self.canvas.fill_circle(center_x, center_y, radius, fill, alpha)

    def rounded_rectangle(
        self,
        box: Box,
        *,
        radius: int,
        fill: Color | None = None,
        outline: Color | None = None,
        width: int = 1,
        alpha: float = 1.0,
    ) -> None:
        if outline is not None:
            self._rounded_fill(box, radius, outline, alpha)
            if fill is not None:
                inset = max(1, int(width))
                left, top, right, bottom = box
                inner = (left + inset, top + inset, right - inset, bottom - inset)
                self._rounded_fill(inner, max(0, radius - inset), fill, alpha)
            return
        if fill is not None:
            self._rounded_fill(box, radius, fill, alpha)

    def ellipse(
        self,
        box: Box,
        *,
        fill: Color,
        alpha: float = 1.0,
    ) -> None:
        left, top, right, bottom = self._box(box)
        radius_x = max(0.0, (right - left) / 2.0)
        radius_y = max(0.0, (bottom - top) / 2.0)
        self.canvas.fill_ellipse(
            left + radius_x, top + radius_y, radius_x, radius_y, fill, alpha
        )

    def line(
        self,
        coordinates: Any,
        *,
        fill: Color,
        width: int = 1,
        joint: str | None = None,
    ) -> None:
        del joint
        values = list(coordinates)
        if len(values) == 4 and not isinstance(values[0], (tuple, list)):
            points = [(values[0], values[1]), (values[2], values[3])]
        else:
            points = values
        for first, second in zip(points, points[1:]):
            self.canvas.line(
                first[0], first[1], second[0], second[1], max(1, width), fill
            )

    def polygon(self, points: list[tuple[int, int]], *, fill: Color) -> None:
        if len(points) < 3:
            return
        anchor = points[0]
        for index in range(1, len(points) - 1):
            second = points[index]
            third = points[index + 1]
            self.canvas.fill_triangle(
                anchor[0],
                anchor[1],
                second[0],
                second[1],
                third[0],
                third[1],
                fill,
            )

    @staticmethod
    def textlength(value: str, *, font: _RasterFont) -> int:
        return len(_raster_text(value)) * 8 * font.scale + (font.scale if font.bold else 0)

    @classmethod
    def textbbox(
        cls,
        position: tuple[int, int],
        value: str,
        *,
        font: _RasterFont,
    ) -> tuple[int, int, int, int]:
        del position
        return (0, 0, cls.textlength(value, font=font), 16 * font.scale)

    def text(
        self,
        position: tuple[int, int],
        value: str,
        *,
        font: _RasterFont,
        fill: Color,
    ) -> None:
        x, y = position
        ascii_value = _raster_text(value)
        if font.bold:
            self.canvas.text_shadow(x, y, ascii_value, fill, scale=font.scale)
        else:
            self.canvas.text(x, y, ascii_value, fill, scale=font.scale)


@dataclass(frozen=True, slots=True)
class GraphicsFrame:
    rgb: bytes
    width: int
    height: int
    columns: int
    rows: int
    content_key: str = "kilix-temps-dashboard"


def kitty_graphics_likely(environment: Mapping[str, str] | None = None) -> bool:
    """Return whether the current terminal advertises Kitty graphics support.

    Kilix sets ``KITTY_WINDOW_ID`` and its streamed sessions set
    ``KILIX_STREAM``.  The TERM checks cover a few terminals that implement the
    protocol without setting Kitty's environment variable.
    """

    env = os.environ if environment is None else environment
    if env.get("KITTY_WINDOW_ID") or env.get("KILIX_STREAM") == "1":
        return True
    signature = " ".join(
        (env.get("TERM", ""), env.get("TERM_PROGRAM", ""))
    ).lower()
    return any(name in signature for name in ("kitty", "ghostty", "wezterm"))


def _presenter_module() -> Any:
    try:
        return importlib.import_module("kitty_frame_presenter")
    except ImportError as original:
        # Source checkouts keep shared libraries in the workspace module
        # umbrella beside kilix-desktops.
        repository_root = Path(__file__).resolve().parents[3]
        candidates = (
            repository_root.parents[1]
            / "kilix-modules/kitty-frame-presenter/src",
            repository_root.parents[1]
            / "kilix/third_party/kitty-frame-presenter/src",
        )
        for candidate in candidates:
            if not (candidate / "kitty_frame_presenter/__init__.py").is_file():
                continue
            candidate_text = str(candidate)
            if candidate_text not in sys.path:
                sys.path.insert(0, candidate_text)
            try:
                return importlib.import_module("kitty_frame_presenter")
            except ImportError:
                continue
        raise GraphicsUnavailable(
            "kitty-frame-presenter is not installed and no sibling checkout was found"
        ) from original


def _soft_raster_module() -> Any:
    try:
        return importlib.import_module("soft_raster")
    except ImportError as original:
        # Development checkouts keep the Python binding beside this repository;
        # packaged installations may provide it normally through site-packages.
        repository_root = Path(__file__).resolve().parents[3]
        candidates = (
            repository_root.parents[1]
            / "kilix-modules/soft-raster/python/src",
            repository_root.parents[1] / "kilix-modules/soft-raster-py/src",
            repository_root.parents[1]
            / "kilix/third_party/soft-raster/python/src",
            repository_root.parents[1] / "kilix/third_party/soft-raster-py/src",
        )
        for candidate in candidates:
            if not (candidate / "soft_raster/__init__.py").is_file():
                continue
            candidate_text = str(candidate)
            if candidate_text not in sys.path:
                sys.path.insert(0, candidate_text)
            try:
                return importlib.import_module("soft_raster")
            except ImportError:
                continue
        raise GraphicsUnavailable(
            "soft-raster-py is not installed and no sibling checkout was found"
        ) from original


def _soft_raster_backend() -> tuple[Any, Any]:
    module = _soft_raster_module()
    error_type = getattr(module, "SoftRasterError", RuntimeError)
    try:
        return module, module.default_library()
    except error_type as original:
        # Preserve an explicit override as authoritative. Otherwise an app
        # checkout can still pair an installed binding with the sibling C
        # library, even if the binding itself lives in site-packages.
        if os.environ.get("SOFT_RASTER_LIBRARY"):
            raise GraphicsUnavailable(f"soft-raster is unavailable: {original}") from original
        repository_root = Path(__file__).resolve().parents[3]
        names = (
            ("soft-raster.dll", "libsoft-raster.dll")
            if sys.platform == "win32"
            else ("libsoft-raster.dylib", "libsoft-raster.so")
            if sys.platform == "darwin"
            else ("libsoft-raster.so",)
        )
        directories = (
            repository_root.parents[1] / "kilix-modules/soft-raster/build",
            repository_root.parents[1] / "kilix/third_party/soft-raster/build",
        )
        for directory in directories:
            for name in names:
                candidate = directory / name
                if not candidate.is_file():
                    continue
                try:
                    return module, module.SoftRasterLibrary(candidate)
                except error_type:
                    continue
        raise GraphicsUnavailable(f"soft-raster is unavailable: {original}") from original


def graphics_available() -> tuple[bool, str]:
    """Report whether the rasterizer and graphical transport can be loaded."""

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
    """Resolve a useful render size from TIOCGWINSZ with a cell-size fallback."""

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
    scale = min(
        1.0,
        maximum[0] / pixel_width,
        maximum[1] / pixel_height,
    )
    return max(160, int(pixel_width * scale)), max(120, int(pixel_height * scale))


class KittyDisplay:
    """Small lifecycle wrapper around the shared Kitty frame presenter."""

    def __init__(
        self,
        terminal: TextIO,
        *,
        max_fps: float = 12.0,
        presenter_class: Any | None = None,
    ) -> None:
        module = _presenter_module()
        presenter_class = presenter_class or module.FramePresenter
        window_id = os.environ.get("KITTY_WINDOW_ID", str(os.getpid()))
        identity = int(window_id) if window_id.isdigit() else os.getpid()
        self.image_id = 5001 + identity % 3000
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
        # Uppercase I removes both the placement and its stored pixel data.
        delete = f"\x1b_Ga=d,d=I,i={self.image_id},q=2\x1b\\"
        if self.stream and self.in_tmux:
            delete = self._wrap_tmux(delete)
        try:
            self.terminal.write(delete)
            self.terminal.flush()
        finally:
            self.presenter.close()


class GraphicalRenderer:
    """Glances-inspired, adaptive pixel renderer for live thermal data."""

    is_graphical = True

    def __init__(self) -> None:
        self._soft_raster, self._library = _soft_raster_backend()
        self.hostname = socket.gethostname()
        self._fonts: dict[tuple[int, bool], _RasterFont] = {}

    def _font(self, size: int, *, bold: bool = False) -> _RasterFont:
        key = (max(7, int(size)), bold)
        cached = self._fonts.get(key)
        if cached is not None:
            return cached
        # The native font is 8x16 and supports integer scaling. Keep dense UI
        # labels at 1x while allowing large values and titles to step up to 2x.
        font = _RasterFont(max(1, (key[0] + 7) // 16), bold)
        self._fonts[key] = font
        return font

    @staticmethod
    def _text_width(draw: Any, text: str, font: Any) -> int:
        try:
            return int(draw.textlength(text, font=font))
        except AttributeError:
            bounds = draw.textbbox((0, 0), text, font=font)
            return bounds[2] - bounds[0]

    def _fit_text(self, draw: Any, text: str, font: Any, width: int) -> str:
        if width <= 0:
            return ""
        if self._text_width(draw, text, font) <= width:
            return text
        suffix = "…"
        if self._text_width(draw, suffix, font) > width:
            return ""
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self._text_width(draw, text[:middle] + suffix, font) <= width:
                low = middle
            else:
                high = middle - 1
        return text[:low] + suffix

    def _text(
        self,
        draw: Any,
        box: Box,
        text: str,
        font: Any,
        fill: Color = TEXT,
        *,
        align: str = "left",
        valign: str = "center",
    ) -> None:
        left, top, right, bottom = box
        value = self._fit_text(draw, text, font, max(0, right - left))
        bounds = draw.textbbox((0, 0), value, font=font)
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        if align == "right":
            x = right - width
        elif align == "center":
            x = left + (right - left - width) // 2
        else:
            x = left
        if valign == "top":
            y = top - bounds[1]
        elif valign == "bottom":
            y = bottom - height - bounds[1]
        else:
            y = top + (bottom - top - height) // 2 - bounds[1]
        draw.text((x, y), value, font=font, fill=fill)

    @staticmethod
    def _panel(draw: Any, box: Box, *, border: Color = BORDER, radius: int = 10) -> None:
        left, top, right, bottom = box
        draw.rounded_rectangle(
            (left + 2, top + 3, right + 2, bottom + 3),
            radius=radius,
            fill=(4, 9, 17),
        )
        draw.rounded_rectangle(box, radius=radius, fill=PANEL, outline=border, width=1)

    @staticmethod
    def _bar(
        draw: Any,
        box: Box,
        ratio: float,
        color: Color,
        *,
        background: Color = PANEL_SOFT,
    ) -> None:
        left, top, right, bottom = box
        ratio = max(0.0, min(1.0, ratio))
        radius = max(2, (bottom - top) // 2)
        draw.rounded_rectangle(box, radius=radius, fill=background)
        filled = left + int((right - left) * ratio)
        if filled > left:
            draw.rounded_rectangle(
                (left, top, max(left + radius * 2, filled), bottom),
                radius=radius,
                fill=color,
            )

    @staticmethod
    def _temperature_ratio(state: SensorState) -> float:
        if state.current is None:
            return 0.0
        floor = min(25.0, state.policy.warning - 30.0)
        return max(
            0.0,
            min(1.0, (state.current - floor) / max(1.0, state.policy.critical - floor)),
        )

    @staticmethod
    def _trend_text(state: SensorState, options: FrameOptions) -> str:
        trend = state.trend_per_minute
        if trend is None:
            return "steady"
        if abs(trend) < 0.1:
            return "steady"
        displayed = abs(options.temperature_unit.delta(trend))
        return f"{'rising' if trend > 0 else 'falling'} {displayed:.1f}°/min"

    def _sparkline(
        self,
        draw: Any,
        box: Box,
        values: list[float],
        low: float,
        high: float,
        color: Color,
        *,
        fill: bool = False,
    ) -> None:
        left, top, right, bottom = box
        width = right - left
        height = bottom - top
        if width < 3 or height < 3 or not values:
            return
        span = max(1.0, high - low)
        points: list[tuple[int, int]] = []
        count = len(values)
        for index, value in enumerate(values):
            x = left + (index * width // max(1, count - 1))
            normalized = max(0.0, min(1.0, (value - low) / span))
            y = bottom - 1 - int(normalized * max(1, height - 2))
            points.append((x, y))
        if fill and len(points) >= 2:
            shade = tuple(max(0, channel // 5) for channel in color)
            draw.polygon(points + [(right, bottom), (left, bottom)], fill=shade)
        if len(points) == 1:
            x, y = points[0]
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=color)
        else:
            draw.line(points, fill=color, width=max(1, height // 16), joint="curve")
            x, y = points[-1]
            dot = max(2, height // 14)
            draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=color)

    @staticmethod
    def _background(image: Any, draw: Any) -> None:
        width, height = image.size
        for y in range(height):
            ratio = y / max(1, height - 1)
            color = tuple(
                int(BG_TOP[index] * (1.0 - ratio) + BG[index] * ratio)
                for index in range(3)
            )
            draw.line((0, y, width, y), fill=color)
        spacing = max(28, width // 32)
        grid = (12, 25, 42)
        for x in range(0, width, spacing):
            draw.line((x, 0, x, height), fill=grid)
        for y in range(0, height, spacing):
            draw.line((0, y, width, y), fill=grid)

    def _header(
        self,
        draw: Any,
        box: Box,
        model: ThermalModel,
        sample: Sample,
        options: FrameOptions,
        scale: float,
    ) -> None:
        left, top, right, bottom = box
        level = model.overall_level
        color = LEVEL_COLORS[level]
        draw.rectangle(box, fill=(11, 28, 49))
        draw.rectangle((left, bottom - 2, right, bottom), fill=color)
        title_font = self._font(19 * scale, bold=True)
        small = self._font(10 * scale)
        label = self._font(9 * scale, bold=True)
        self._text(
            draw,
            (left + 18, top + 4, right // 2, bottom - 4),
            "KILIX TUI",
            title_font,
            WHITE,
        )
        self._text(
            draw,
            (left + 18, bottom - int(20 * scale), right // 2, bottom - 4),
            f"{self.hostname}  •  THERMAL WATCH",
            small,
            MUTED,
            valign="bottom",
        )
        mode = "PAUSED" if options.paused else f"LIVE  {options.interval:.1f}s"
        log = "  •  LOGGING" if options.logging else ""
        clock = sample.timestamp.strftime("%a %d %b  %H:%M:%S")
        status = f"{mode}  •  {options.temperature_unit.symbol}{log}    {clock}"
        status_width = self._text_width(draw, status, label) + int(48 * scale)
        pill = (
            max(left + 190, right - status_width - 16),
            top + int(12 * scale),
            right - 16,
            bottom - int(12 * scale),
        )
        draw.rounded_rectangle(pill, radius=(pill[3] - pill[1]) // 2, fill=(18, 43, 69))
        dot = max(3, int(4 * scale))
        cy = (pill[1] + pill[3]) // 2
        draw.ellipse(
            (pill[0] + 10, cy - dot, pill[0] + 10 + dot * 2, cy + dot),
            fill=color,
        )
        self._text(
            draw,
            (pill[0] + 18 + dot, pill[1], pill[2] - 10, pill[3]),
            status,
            label,
            TEXT,
            align="right",
        )

    def _summary_card(
        self,
        draw: Any,
        box: Box,
        title: str,
        value: str,
        detail: str,
        ratio: float,
        color: Color,
        scale: float,
        *,
        history: list[float] | None = None,
        history_high: float = 100.0,
    ) -> None:
        self._panel(draw, box, border=tuple(max(a, b // 2) for a, b in zip(BORDER, color)))
        left, top, right, bottom = box
        pad = max(9, int(13 * scale))
        label_font = self._font(9 * scale, bold=True)
        value_font = self._font(25 * scale, bold=True)
        detail_font = self._font(9 * scale)
        self._text(
            draw,
            (left + pad, top + pad // 2, right - pad, top + pad + int(15 * scale)),
            title.upper(),
            label_font,
            MUTED,
            valign="top",
        )
        value_bottom = top + int((bottom - top) * 0.68)
        self._text(
            draw,
            (left + pad, top + int(22 * scale), right - pad, value_bottom),
            value,
            value_font,
            color,
            valign="bottom",
        )
        detail_right = right - pad
        if history:
            chart_width = max(36, int((right - left) * 0.35))
            chart = (
                right - pad - chart_width,
                top + int(28 * scale),
                right - pad,
                value_bottom - 2,
            )
            self._sparkline(draw, chart, history, 25.0, history_high, color, fill=True)
            detail_right = right - pad
        bar_top = bottom - max(8, int(11 * scale))
        self._bar(
            draw,
            (left + pad, bar_top, right - pad, bar_top + max(3, int(4 * scale))),
            ratio,
            color,
        )
        self._text(
            draw,
            (left + pad, value_bottom + 1, detail_right, bar_top - 2),
            detail,
            detail_font,
            MUTED,
            valign="top",
        )

    def _summary(
        self,
        draw: Any,
        box: Box,
        model: ThermalModel,
        sample: Sample,
        options: FrameOptions,
        scale: float,
        columns: int,
    ) -> None:
        left, top, right, bottom = box
        gap = max(7, int(10 * scale))
        metrics = sample.metrics
        risk = model.most_at_risk
        cards: list[tuple[str, str, str, float, Color, list[float] | None, float]] = []
        if risk is None or risk.current is None:
            cards.append(("Hottest", "--", "waiting for sensors", 0.0, MUTED, None, 100.0))
            cards.append(("Headroom", "--", "no policy data", 0.0, MUTED, None, 100.0))
        else:
            color = LEVEL_COLORS[risk.level]
            unit = options.temperature_unit
            cards.append(
                (
                    "Hottest",
                    f"{unit.absolute(risk.current):.1f}{unit.symbol}",
                    risk.sensor.display_name,
                    self._temperature_ratio(risk),
                    color,
                    risk.values,
                    risk.policy.critical,
                )
            )
            room = risk.headroom or 0.0
            room_ratio = max(0.0, min(1.0, room / max(1.0, risk.policy.critical - 25.0)))
            cards.append(
                (
                    "Headroom",
                    f"{unit.delta(room):.1f}{unit.symbol}",
                    self._trend_text(risk, options),
                    room_ratio,
                    color,
                    None,
                    100.0,
                )
            )
        cpu = metrics.cpu_percent
        cpu_value = "warmup" if cpu is None else f"{cpu:.0f}%"
        cpu_ratio = (
            min(1.0, metrics.load_1 / max(1, metrics.cpu_count))
            if cpu is None
            else cpu / 100.0
        )
        cards.append(
            (
                "CPU",
                cpu_value,
                f"load {metrics.load_1:.2f}  {metrics.load_5:.2f}  {metrics.load_15:.2f}",
                cpu_ratio,
                BLUE,
                None,
                100.0,
            )
        )
        memory = metrics.memory_percent
        cards.append(
            (
                "Memory",
                "--" if memory is None else f"{memory:.0f}%",
                f"uptime {format_duration(metrics.uptime_seconds)}",
                0.0 if memory is None else memory / 100.0,
                BLUE,
                None,
                100.0,
            )
        )

        card_columns = 4 if columns >= 72 and right - left >= 720 else 2
        card_rows = 1 if card_columns == 4 else 2
        card_width = (right - left - gap * (card_columns - 1)) // card_columns
        card_height = (bottom - top - gap * (card_rows - 1)) // card_rows
        for index, card in enumerate(cards):
            row, column = divmod(index, card_columns)
            x = left + column * (card_width + gap)
            y = top + row * (card_height + gap)
            card_box = (x, y, x + card_width, y + card_height)
            self._summary_card(
                draw,
                card_box,
                card[0],
                card[1],
                card[2],
                card[3],
                card[4],
                scale,
                history=card[5],
                history_high=card[6],
            )

    def _section_title(
        self,
        draw: Any,
        box: Box,
        title: str,
        detail: str,
        scale: float,
        *,
        color: Color = BLUE,
    ) -> None:
        left, top, right, bottom = box
        title_font = self._font(11 * scale, bold=True)
        detail_font = self._font(9 * scale)
        draw.rectangle((left, top, left + max(3, int(4 * scale)), bottom), fill=color)
        self._text(
            draw,
            (left + int(12 * scale), top, (left + right) // 2, bottom),
            title.upper(),
            title_font,
            TEXT,
        )
        self._text(
            draw,
            ((left + right) // 2, top, right, bottom),
            detail,
            detail_font,
            MUTED,
            align="right",
        )

    def _sensor_table(
        self,
        draw: Any,
        box: Box,
        model: ThermalModel,
        options: FrameOptions,
        scale: float,
    ) -> None:
        self._panel(draw, box)
        left, top, right, bottom = box
        pad = max(8, int(12 * scale))
        title_height = max(25, int(31 * scale))
        states = model.ordered_states(options.sort_mode)
        body_top = top + pad + title_height
        body_bottom = bottom - pad
        available = max(1, body_bottom - body_top)
        row_height = max(25, int(34 * scale))
        header_height = max(18, int(22 * scale))
        capacity = max(1, (available - header_height) // row_height)
        maximum_scroll = max(0, len(states) - capacity)
        scroll = min(maximum_scroll, max(0, options.scroll))
        shown = states[scroll : scroll + capacity]
        start = scroll + 1 if states else 0
        end = scroll + len(shown)
        self._section_title(
            draw,
            (left + pad, top + pad, right - pad, top + pad + title_height - 4),
            "Thermal sensors",
            f"{start}–{end} / {len(states)}   {options.temperature_unit.symbol}   "
            f"sort {options.sort_mode}",
            scale,
            color=LEVEL_COLORS[model.overall_level],
        )

        table_left = left + pad
        table_right = right - pad
        table_width = table_right - table_left
        medium = table_width >= 550
        wide = table_width >= 760
        small = self._font(8 * scale, bold=True)
        body = self._font(9 * scale)
        body_bold = self._font(10 * scale, bold=True)
        header_box = (table_left, body_top, table_right, body_top + header_height)
        draw.rounded_rectangle(header_box, radius=4, fill=(19, 34, 54))

        name_right = table_left + int(table_width * (0.34 if wide else 0.40))
        now_right = name_right + int(table_width * 0.12)
        if wide:
            min_right = now_right + int(table_width * 0.10)
            peak_right = min_right + int(table_width * 0.10)
            room_right = peak_right + int(table_width * 0.12)
        elif medium:
            min_right = now_right
            peak_right = now_right
            room_right = now_right + int(table_width * 0.16)
        else:
            min_right = peak_right = now_right
            room_right = now_right + int(table_width * 0.18)
        columns = [
            (table_left + 8, name_right - 4, "SENSOR", "left"),
            (name_right, now_right - 4, "NOW", "right"),
        ]
        if wide:
            columns.extend(
                [
                    (now_right, min_right - 4, "MIN", "right"),
                    (min_right, peak_right - 4, "PEAK", "right"),
                ]
            )
        columns.append((peak_right, room_right - 4, "ROOM", "right"))
        columns.append((room_right + 8, table_right - 8, "HISTORY", "left"))
        for x1, x2, label, align in columns:
            self._text(draw, (x1, header_box[1], x2, header_box[3]), label, small, MUTED, align=align)

        y = header_box[3]
        if not shown:
            self._text(
                draw,
                (table_left, y, table_right, body_bottom),
                "No readable Linux temperature sensors",
                body,
                MUTED,
                align="center",
            )
            return

        for row_index, state in enumerate(shown):
            row_top = y + row_index * row_height
            row_bottom = min(body_bottom, row_top + row_height)
            if row_index % 2:
                draw.rectangle((table_left, row_top, table_right, row_bottom), fill=(16, 29, 47))
            color = LEVEL_COLORS[state.level]
            dot = max(3, int(4 * scale))
            cy = (row_top + row_bottom) // 2
            draw.ellipse(
                (table_left + 8, cy - dot, table_left + 8 + dot * 2, cy + dot),
                fill=color,
            )
            name_left = table_left + 18 + dot
            self._text(
                draw,
                (name_left, row_top, name_right - 5, row_bottom),
                state.sensor.display_name,
                body,
                TEXT,
            )
            unit = options.temperature_unit
            current = (
                "--"
                if state.current is None
                else f"{unit.absolute(state.current):.1f}°"
            )
            self._text(draw, (name_right, row_top, now_right - 4, row_bottom), current, body_bold, color, align="right")
            if wide:
                minimum = state.minimum if state.minimum is not None else state.current
                maximum = state.maximum if state.maximum is not None else state.current
                self._text(draw, (now_right, row_top, min_right - 4, row_bottom), "--" if minimum is None else f"{unit.absolute(minimum):.1f}°", body, MUTED, align="right")
                self._text(draw, (min_right, row_top, peak_right - 4, row_bottom), "--" if maximum is None else f"{unit.absolute(maximum):.1f}°", body, TEXT, align="right")
            room = state.headroom
            self._text(draw, (peak_right, row_top, room_right - 4, row_bottom), "--" if room is None else f"{unit.delta(room):+.1f}°", body, color, align="right")
            chart = (
                room_right + 8,
                row_top + max(5, row_height // 5),
                table_right - 8,
                row_bottom - max(5, row_height // 5),
            )
            threshold_y = chart[3] - int(
                max(0.0, min(1.0, (state.policy.warning - 25.0) / max(1.0, state.policy.critical - 25.0)))
                * max(1, chart[3] - chart[1])
            )
            draw.line((chart[0], threshold_y, chart[2], threshold_y), fill=(64, 58, 42), width=1)
            self._sparkline(draw, chart, state.values, 25.0, state.policy.critical, color)

    def _system_panel(
        self,
        draw: Any,
        box: Box,
        sample: Sample,
        fan_specs: list[FanSensor],
        scale: float,
    ) -> None:
        self._panel(draw, box)
        left, top, right, bottom = box
        pad = max(8, int(12 * scale))
        title_h = max(24, int(29 * scale))
        self._section_title(
            draw,
            (left + pad, top + pad, right - pad, top + pad + title_h - 4),
            "System pulse",
            f"{sample.metrics.cpu_count} threads",
            scale,
            color=BLUE,
        )
        label = self._font(8 * scale, bold=True)
        value = self._font(9 * scale)
        y = top + pad + title_h + 4
        row_h = max(24, int(31 * scale))
        metrics = sample.metrics
        gauges = [
            ("CPU", metrics.cpu_percent, BLUE),
            ("MEM", metrics.memory_percent, BLUE),
        ]
        for name, number, color in gauges:
            if y + row_h > bottom - pad:
                break
            display = "--" if number is None else f"{number:.0f}%"
            self._text(draw, (left + pad, y, left + pad + 42, y + row_h), name, label, MUTED)
            self._bar(
                draw,
                (left + pad + 45, y + row_h // 2 - 3, right - pad - 40, y + row_h // 2 + 3),
                0.0 if number is None else number / 100.0,
                color,
            )
            self._text(draw, (right - pad - 36, y, right - pad, y + row_h), display, value, TEXT, align="right")
            y += row_h

        load_text = f"LOAD  {metrics.load_1:.2f}  {metrics.load_5:.2f}  {metrics.load_15:.2f}"
        if y + row_h <= bottom - pad:
            self._text(draw, (left + pad, y, right - pad, y + row_h), load_text, value, TEXT)
            y += row_h

        fan_names = {fan.key: fan for fan in fan_specs}
        for key, rpm in sample.fans.items():
            if y + row_h > bottom - pad:
                break
            spec = fan_names.get(key)
            name = spec.label if spec else key
            self._text(draw, (left + pad, y, right - pad - 80, y + row_h), f"FAN  {name}", value, MUTED)
            self._text(draw, (right - pad - 80, y, right - pad, y + row_h), f"{rpm:,} RPM", value, BLUE, align="right")
            y += row_h

    def _process_panel(
        self,
        draw: Any,
        box: Box,
        sample: Sample,
        scale: float,
    ) -> None:
        self._panel(draw, box)
        left, top, right, bottom = box
        pad = max(8, int(12 * scale))
        title_h = max(24, int(29 * scale))
        processes = sample.metrics.top_processes
        self._section_title(
            draw,
            (left + pad, top + pad, right - pad, top + pad + title_h - 4),
            "Heat sources",
            "CPU consumers",
            scale,
            color=RED,
        )
        body = self._font(9 * scale)
        small = self._font(8 * scale)
        y = top + pad + title_h + 2
        available = bottom - pad - y
        if not processes:
            self._text(
                draw,
                (left + pad, y, right - pad, bottom - pad),
                "Collecting a 2-second CPU baseline…",
                body,
                MUTED,
                align="center",
            )
            return
        row_h = max(25, min(int(35 * scale), available // max(1, len(processes))))
        maximum = max(process.cpu_percent for process in processes)
        for process in processes:
            if y + row_h > bottom - pad + 1:
                break
            instances = f" ×{process.instances}" if process.instances > 1 else ""
            self._text(draw, (left + pad, y, right - pad - 55, y + row_h // 2 + 3), process.name + instances, body, TEXT)
            self._text(draw, (right - pad - 52, y, right - pad, y + row_h // 2 + 3), f"{process.cpu_percent:.0f}%", small, RED, align="right")
            self._bar(
                draw,
                (left + pad, y + row_h - 6, right - pad, y + row_h - 2),
                process.cpu_percent / max(1.0, maximum),
                RED,
                background=(43, 32, 28),
            )
            y += row_h

    def _guard(
        self,
        draw: Any,
        box: Box,
        model: ThermalModel,
        sample: Sample,
        options: FrameOptions,
        scale: float,
    ) -> None:
        level = model.overall_level
        color = LEVEL_COLORS[level]
        left, top, right, bottom = box
        draw.rounded_rectangle(box, radius=8, fill=(15, 27, 43), outline=color, width=1)
        draw.rounded_rectangle((left, top, left + max(6, int(8 * scale)), bottom), radius=8, fill=color)
        font = self._font(9 * scale, bold=True)
        small = self._font(8 * scale)
        if level >= Level.HOT:
            message = "Reduce heavy work and inspect cooling — thermal headroom is low."
        elif level == Level.WARM:
            message = "Thermals are elevated; watch the trend and cooling response."
        else:
            message = "Thermal headroom is healthy."
        self._text(draw, (left + 18, top, (left + right) // 2 + 70, bottom), f"THERMAL GUARD  {level.label}  •  {message}", font, color)
        event = options.notice
        if not event and model.alerts:
            alert = model.alerts[0]
            ago = max(0, int(sample.monotonic - alert.monotonic))
            value = options.temperature_unit.absolute(alert.value)
            event = (
                f"last crossing {alert.sensor_name}  "
                f"{value:.1f}{options.temperature_unit.symbol}  {ago}s ago"
            )
        if not event:
            event = "no HOT / CRITICAL crossings this session"
        self._text(draw, ((left + right) // 2 + 70, top, right - 12, bottom), event, small, MUTED, align="right")

    def _footer(self, draw: Any, box: Box, options: FrameOptions, scale: float) -> None:
        left, top, right, bottom = box
        font = self._font(8 * scale)
        controls = "q quit   space pause   r reset   +/− rate   ↑↓ scroll   s sort   u unit   l log   h help"
        self._text(draw, (left + 4, top, right - 180, bottom), controls, font, MUTED)
        log = options.notice or ("CSV LOG ON" if options.logging else "CSV LOG OFF")
        self._text(draw, (right - 180, top, right - 4, bottom), log, font, BLUE if options.logging else DIM, align="right")

    def _compact(
        self,
        image: Any,
        draw: Any,
        model: ThermalModel,
        sample: Sample,
        fan_specs: list[FanSensor],
        columns: int,
        rows: int,
        options: FrameOptions,
        scale: float,
    ) -> None:
        width, height = image.size
        header_h = max(38, min(52, height // 5))
        self._header(draw, (0, 0, width, header_h), model, sample, options, scale)
        margin = max(6, int(9 * scale))
        footer_h = max(22, int(28 * scale))
        risk = model.most_at_risk
        risk_h = max(58, min(82, (height - header_h - footer_h) // 3))
        box = (margin, header_h + margin, width - margin, header_h + margin + risk_h)
        self._panel(draw, box, border=LEVEL_COLORS[model.overall_level], radius=8)
        title = self._font(8 * scale, bold=True)
        big = self._font(22 * scale, bold=True)
        body = self._font(8 * scale)
        if risk is None or risk.current is None:
            self._text(draw, (box[0] + 10, box[1], box[2] - 10, box[3]), "NO SENSOR DATA", big, MUTED, align="center")
        else:
            color = LEVEL_COLORS[risk.level]
            unit = options.temperature_unit
            self._text(draw, (box[0] + 12, box[1] + 3, box[2] // 2, box[1] + 24), "HOTTEST SENSOR", title, MUTED)
            self._text(draw, (box[0] + 12, box[1] + 20, box[2] // 2, box[3] - 5), f"{unit.absolute(risk.current):.1f}{unit.symbol}", big, color)
            self._text(draw, (box[2] // 2, box[1] + 5, box[2] - 12, (box[1] + box[3]) // 2), risk.sensor.display_name, body, TEXT, align="right")
            self._text(draw, (box[2] // 2, (box[1] + box[3]) // 2, box[2] - 12, box[3] - 5), f"room {unit.delta(risk.headroom or 0.0):.1f}°  •  {risk.level.label}", title, color, align="right")

        table_top = box[3] + margin
        table_bottom = height - footer_h - margin
        table = (margin, table_top, width - margin, table_bottom)
        self._panel(draw, table, radius=8)
        states = model.ordered_states(options.sort_mode)
        row_h = max(22, int(27 * scale))
        capacity = max(1, (table_bottom - table_top - 22) // row_h)
        maximum_scroll = max(0, len(states) - capacity)
        scroll = min(maximum_scroll, max(0, options.scroll))
        shown = states[scroll : scroll + capacity]
        self._text(draw, (table[0] + 10, table[1], table[2] - 10, table[1] + 22), f"SENSORS  {scroll + 1 if shown else 0}–{scroll + len(shown)} / {len(states)}  {options.temperature_unit.symbol}", title, MUTED)
        y = table[1] + 22
        for index, state in enumerate(shown):
            row_bottom = min(table[3] - 3, y + row_h)
            if index % 2:
                draw.rectangle((table[0] + 3, y, table[2] - 3, row_bottom), fill=(16, 29, 47))
            color = LEVEL_COLORS[state.level]
            draw.ellipse((table[0] + 10, (y + row_bottom) // 2 - 3, table[0] + 16, (y + row_bottom) // 2 + 3), fill=color)
            self._text(draw, (table[0] + 22, y, table[2] - 120, row_bottom), state.sensor.display_name, body, TEXT)
            self._text(draw, (table[2] - 116, y, table[2] - 58, row_bottom), f"{options.temperature_unit.absolute(state.current):.1f}°", title, color, align="right")
            self._text(draw, (table[2] - 54, y, table[2] - 10, row_bottom), state.level.label, body, color, align="right")
            y += row_h
        self._footer(draw, (margin, height - footer_h, width - margin, height), options, scale)

    def _help_overlay(
        self,
        image: Any,
        draw: Any,
        model: ThermalModel,
        options: FrameOptions,
        scale: float,
    ) -> None:
        width, height = image.size
        draw.rectangle((0, 0, width, height), fill=(3, 8, 15), alpha=0.70)
        panel_width = min(width - 28, max(390, int(width * 0.68)))
        panel_height = min(height - 24, max(260, int(height * 0.72)))
        left = (width - panel_width) // 2
        top = (height - panel_height) // 2
        box = (left, top, left + panel_width, top + panel_height)
        self._panel(draw, box, border=BLUE, radius=12)
        title = self._font(18 * scale, bold=True)
        key_font = self._font(9 * scale, bold=True)
        body = self._font(9 * scale)
        self._text(draw, (left + 22, top + 14, box[2] - 22, top + 52), "KILIX TUI · TEMPERATURE HELP", title, WHITE)
        entries = [
            ("q / Esc", "quit and restore the terminal"),
            ("Space", "pause or resume sampling"),
            ("r", "reset minimum, peak, and graph history"),
            ("+ / −", "sample faster or slower"),
            ("↑ / ↓  j / k", "scroll the sensor table"),
            ("s", "toggle risk/source sorting"),
            ("u", "toggle Fahrenheit/Celsius display"),
            ("l", "toggle private CSV logging"),
            ("h / ?", "close this help"),
        ]
        row_h = max(22, int(27 * scale))
        y = top + 60
        key_width = min(150, panel_width // 3)
        for key, description in entries:
            if y + row_h > box[3] - 58:
                break
            self._text(draw, (left + 22, y, left + 22 + key_width, y + row_h), key, key_font, BLUE)
            self._text(draw, (left + 30 + key_width, y, box[2] - 22, y + row_h), description, body, TEXT)
            y += row_h
        policy = (
            f"WARM {options.temperature_unit.absolute(model.thresholds.warning):.0f}{options.temperature_unit.symbol}   "
            f"HOT {options.temperature_unit.absolute(model.thresholds.hot):.0f}{options.temperature_unit.symbol}   "
            f"LIMIT {options.temperature_unit.absolute(model.thresholds.critical):.0f}{options.temperature_unit.symbol}"
        )
        self._text(draw, (left + 22, box[3] - 51, box[2] - 22, box[3] - 31), policy, key_font, WHITE)
        self._text(draw, (left + 22, box[3] - 30, box[2] - 22, box[3] - 10), "Monitoring only — Kilix Temps never throttles, kills jobs, or powers off.", body, MUTED)

    def render(
        self,
        model: ThermalModel,
        sample: Sample,
        fan_specs: list[FanSensor],
        columns: int,
        rows: int,
        options: FrameOptions,
        *,
        pixel_size: tuple[int, int] | None = None,
    ) -> GraphicsFrame:
        width, height = pixel_size or (max(160, columns * 10), max(120, rows * 20))
        image = self._soft_raster.Canvas(width, height, library=self._library)
        try:
            image.clear(BG)
            draw = _RasterDraw(image)
            self._background(image, draw)
            scale = max(0.72, min(1.35, width / 1050.0, height / 620.0))

            if columns < 24 or rows < 8:
                title = self._font(18 * scale, bold=True)
                body = self._font(9 * scale)
                self._text(draw, (10, 0, width - 10, height // 2 + 4), "KILIX TEMPS", title, WHITE, align="center", valign="bottom")
                self._text(draw, (10, height // 2 + 6, width - 10, height), "pane needs 24 × 8 cells", body, MUTED, align="center", valign="top")
            elif width < 560 or height < 330 or rows < 16:
                self._compact(image, draw, model, sample, fan_specs, columns, rows, options, scale)
            else:
                margin = max(9, int(13 * scale))
                gap = max(8, int(11 * scale))
                header_h = max(52, min(72, int(height * 0.10)))
                footer_h = max(24, int(30 * scale))
                guard_h = max(34, int(42 * scale))
                card_rows = 1 if columns >= 72 and width >= 720 else 2
                summary_h = max(
                    100 if card_rows == 1 else 176,
                    int(height * (0.21 if card_rows == 1 else 0.31)),
                )
                maximum_summary = max(90, height - header_h - footer_h - guard_h - 150)
                summary_h = min(summary_h, maximum_summary)

                self._header(draw, (0, 0, width, header_h), model, sample, options, scale)
                summary_top = header_h + margin
                self._summary(
                    draw,
                    (margin, summary_top, width - margin, summary_top + summary_h),
                    model,
                    sample,
                    options,
                    scale,
                    columns,
                )
                footer_top = height - footer_h
                guard_bottom = footer_top - max(4, gap // 2)
                guard_top = guard_bottom - guard_h
                body_top = summary_top + summary_h + gap
                body_bottom = guard_top - gap
                body_height = max(1, body_bottom - body_top)
                wide = width >= 850 and columns >= 78
                if wide:
                    side_width = max(245, int((width - margin * 2 - gap) * 0.31))
                    sensor_right = width - margin - side_width - gap
                    self._sensor_table(draw, (margin, body_top, sensor_right, body_bottom), model, options, scale)
                    side_left = sensor_right + gap
                    system_h = max(130, int(body_height * 0.47))
                    system_h = min(body_height - 70, system_h)
                    self._system_panel(draw, (side_left, body_top, width - margin, body_top + system_h), sample, fan_specs, scale)
                    self._process_panel(draw, (side_left, body_top + system_h + gap, width - margin, body_bottom), sample, scale)
                else:
                    self._sensor_table(draw, (margin, body_top, width - margin, body_bottom), model, options, scale)
                self._guard(draw, (margin, guard_top, width - margin, guard_bottom), model, sample, options, scale)
                self._footer(draw, (margin, footer_top, width - margin, height), options, scale)

            if options.help_visible:
                self._help_overlay(image, draw, model, options, scale)
            rgb = image.rgb_bytes()
        finally:
            image.close()
        return GraphicsFrame(
            rgb=rgb,
            width=width,
            height=height,
            columns=max(1, columns),
            rows=max(1, rows),
        )
