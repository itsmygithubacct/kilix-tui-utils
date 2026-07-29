"""Pixel rendering and Kitty graphics transport for the desktop.

The same arrangement Kilix Temps and Kilix Memory proved: the renderer knows
nothing about terminal modes or input — it produces complete RGB frames from
the desktop's state — and a small display wrapper hands those frames to the
damage-aware presenter shared across Kilix's graphical applications. Both the
rasterizer and the presenter are optional; `available()` is the gate and the
curses layout in `desk.py` is what everything degrades to.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import importlib
import os
from pathlib import Path
import struct
import sys
import termios
import time
from typing import Any, Mapping, TextIO

from . import tango
from .desk import SECTIONS, State, entry_hint, footer as footer_text, \
    visible_window

Color = tuple[int, int, int]
Box = tuple[int, int, int, int]


class GraphicsUnavailable(RuntimeError):
    """Raised when the optional graphical path cannot be used."""


def kitty_graphics_likely(environment: Mapping[str, str] | None = None) -> bool:
    """Whether the terminal advertises the Kitty graphics protocol."""
    env = os.environ if environment is None else environment
    if env.get("KITTY_WINDOW_ID") or env.get("KILIX_STREAM") == "1":
        return True
    signature = " ".join(
        (env.get("TERM", ""), env.get("TERM_PROGRAM", ""))).lower()
    return any(name in signature for name in ("kitty", "ghostty", "wezterm"))


def _presenter_module() -> Any:
    try:
        return importlib.import_module("kitty_frame_presenter")
    except ImportError as original:
        package_root = Path(__file__).resolve().parents[2]
        candidates = (
            package_root.parent / "kitty-frame-presenter/src",
            package_root.parent / "kilix/third_party/kitty-frame-presenter/src",
        )
        for candidate in candidates:
            if not (candidate / "kitty_frame_presenter/__init__.py").is_file():
                continue
            text = str(candidate)
            if text not in sys.path:
                sys.path.insert(0, text)
            try:
                return importlib.import_module("kitty_frame_presenter")
            except ImportError:
                continue
        raise GraphicsUnavailable(
            "kitty-frame-presenter is not installed and no sibling checkout "
            "was found") from original


def _soft_raster_backend() -> tuple[Any, Any]:
    try:
        module = importlib.import_module("soft_raster")
    except ImportError as original:
        package_root = Path(__file__).resolve().parents[2]
        candidates = (
            package_root.parent / "soft-raster-py/src",
            package_root.parent / "kilix/third_party/soft-raster-py/src",
        )
        module = None
        for candidate in candidates:
            if not (candidate / "soft_raster/__init__.py").is_file():
                continue
            text = str(candidate)
            if text not in sys.path:
                sys.path.insert(0, text)
            try:
                module = importlib.import_module("soft_raster")
                break
            except ImportError:
                continue
        if module is None:
            raise GraphicsUnavailable(
                "soft-raster-py is not installed and no sibling checkout "
                "was found") from original
    error_type = getattr(module, "SoftRasterError", RuntimeError)
    try:
        return module, module.default_library()
    except error_type as original:
        if os.environ.get("SOFT_RASTER_LIBRARY"):
            raise GraphicsUnavailable(
                f"soft-raster is unavailable: {original}") from original
        package_root = Path(__file__).resolve().parents[2]
        names = (("libsoft-raster.so",) if sys.platform.startswith("linux")
                 else ("libsoft-raster.dylib", "libsoft-raster.so"))
        directories = (
            package_root.parent / "soft-raster/build",
            package_root.parent / "kilix/third_party/soft-raster/build",
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
        raise GraphicsUnavailable(
            f"soft-raster is unavailable: {original}") from original


def available() -> tuple[bool, str]:
    """Report whether the rasterizer and the transport can both be loaded."""
    try:
        _soft_raster_backend()
        _presenter_module()
    except GraphicsUnavailable as error:
        return False, str(error)
    return True, ""


def terminal_pixel_size(
    fd: int, columns: int, rows: int, *,
    maximum: tuple[int, int] = (1680, 1050),
) -> tuple[int, int]:
    """A useful render size from TIOCGWINSZ, with a cell-size fallback."""
    pixel_width = 0
    pixel_height = 0
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        ioctl_rows, ioctl_columns, pixel_width, pixel_height = struct.unpack(
            "HHHH", packed)
        if ioctl_columns and ioctl_rows:
            columns, rows = ioctl_columns, ioctl_rows
    except (OSError, ValueError, struct.error):
        pass
    if pixel_width < columns * 4 or pixel_height < rows * 8:
        pixel_width = columns * 10
        pixel_height = rows * 20
    pixel_width = max(200, pixel_width)
    pixel_height = max(140, pixel_height)
    scale = min(1.0, maximum[0] / pixel_width, maximum[1] / pixel_height)
    return max(200, int(pixel_width * scale)), max(140, int(pixel_height * scale))


@dataclass(frozen=True, slots=True)
class GraphicsFrame:
    rgb: bytes
    width: int
    height: int
    columns: int
    rows: int
    content_key: str = "kilix-tui-desktop"


class KittyDisplay:
    """Lifecycle wrapper around the shared Kitty frame presenter."""

    def __init__(self, terminal: TextIO, *, max_fps: float = 12.0,
                 presenter_class: Any | None = None) -> None:
        module = _presenter_module()
        presenter_class = presenter_class or module.FramePresenter
        window_id = os.environ.get("KITTY_WINDOW_ID", str(os.getpid()))
        identity = int(window_id) if window_id.isdigit() else os.getpid()
        self.image_id = 5601 + identity % 3000
        self.terminal = terminal
        self.stream = os.environ.get("KILIX_STREAM") == "1"
        self.in_tmux = bool(os.environ.get("TMUX"))
        self._wrap_tmux = module.wrap_tmux_passthrough
        self.presenter = presenter_class(
            terminal, image_id=self.image_id, stream=self.stream,
            in_tmux=self.in_tmux, max_fps=max_fps, stream_warmup_seconds=0)

    @property
    def next_deadline(self) -> float | None:
        return self.presenter.next_deadline

    def present(self, frame: GraphicsFrame, *, force_full: bool = False) -> Any:
        return self.presenter.present(
            frame.rgb, frame.width, frame.height, frame.columns, frame.rows,
            content_key=frame.content_key, force_full=force_full)

    def invalidate(self) -> None:
        self.presenter.invalidate()

    def flush(self) -> Any:
        return self.presenter.flush()

    def hide(self) -> None:
        """Remove the placement and pixel data; used around a hand-off."""
        delete = f"\x1b_Ga=d,d=I,i={self.image_id},q=2\x1b\\"
        if self.stream and self.in_tmux:
            delete = self._wrap_tmux(delete)
        self.terminal.write(delete)
        self.terminal.flush()
        self.presenter.invalidate()

    def close(self) -> None:
        try:
            self.hide()
        finally:
            self.presenter.close()


# ── drawing facade over a soft-raster canvas ─────────────────────────────────


@dataclass(frozen=True, slots=True)
class Font:
    """soft-raster's embedded 8x16 font at an integer scale."""

    scale: int
    bold: bool = False

    def width(self, text: str) -> int:
        return len(text) * 8 * self.scale + (self.scale if self.bold else 0)

    @property
    def height(self) -> int:
        return 16 * self.scale


def font_for(size: float, *, bold: bool = False) -> Font:
    """The nearest integer scale for a nominal pixel size.

    The native glyphs are 8x16; sizes step to 1x, 2x, 3x… so "larger font"
    means exactly that, and a narrow pane steps back down to 1x — the same
    number the text fallback effectively renders at.
    """
    return Font(max(1, (int(size) + 7) // 16), bold)


_ASCII = str.maketrans({
    "\N{BULLET}": "|", "\N{HORIZONTAL ELLIPSIS}": "~",
    "\N{EM DASH}": "-", "\N{EN DASH}": "-",
    "\N{BLACK RIGHT-POINTING TRIANGLE}": ">",
    "\N{BLACK RIGHT-POINTING SMALL TRIANGLE}": ">",
    "\N{BLACK LEFT-POINTING SMALL TRIANGLE}": "<",
    "\N{UPWARDS ARROW}": "^", "\N{DOWNWARDS ARROW}": "v",
    "\N{LEFTWARDS ARROW}": "<", "\N{RIGHTWARDS ARROW}": ">",
    "\N{MIDDLE DOT}": ".",
})


def _ascii(value: str) -> str:
    return value.translate(_ASCII).encode("ascii", "replace").decode("ascii")


class Draw:
    """Box-oriented drawing helpers over a soft-raster canvas."""

    def __init__(self, canvas: Any) -> None:
        self.canvas = canvas

    def fill(self, box: Box, color: Color, alpha: float = 1.0) -> None:
        left, top, right, bottom = box
        if right > left and bottom > top:
            self.canvas.fill_rect(left, top, right - left, bottom - top,
                                  color, alpha)

    def rounded(self, box: Box, radius: int, color: Color,
                alpha: float = 1.0) -> None:
        left, top, right, bottom = box
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return
        radius = max(0, min(int(radius), width // 2, height // 2))
        if radius <= 1:
            self.canvas.fill_rect(left, top, width, height, color, alpha)
            return
        self.canvas.fill_rect(left + radius, top, width - radius * 2, height,
                              color, alpha)
        self.canvas.fill_rect(left, top + radius, width,
                              height - radius * 2, color, alpha)
        for cx, cy in ((left + radius, top + radius),
                       (right - radius, top + radius),
                       (left + radius, bottom - radius),
                       (right - radius, bottom - radius)):
            self.canvas.fill_circle(cx, cy, radius, color, alpha)

    def panel(self, box: Box, *, radius: int = 12, fill: Color = tango.CARD,
              edge: Color = tango.CARD_EDGE) -> None:
        left, top, right, bottom = box
        self.rounded((left + 3, top + 4, right + 3, bottom + 4), radius,
                     tango.CARD_SHADOW, 0.55)
        self.rounded(box, radius, edge)
        self.rounded((left + 1, top + 1, right - 1, bottom - 1),
                     max(0, radius - 1), fill)

    def hline(self, x1: int, x2: int, y: int, color: Color,
              width: int = 1) -> None:
        if x2 > x1:
            self.canvas.fill_rect(x1, y, x2 - x1, width, color)

    def fit(self, text: str, font: Font, width: int) -> str:
        text = _ascii(text)
        if font.width(text) <= width:
            return text
        cell = 8 * font.scale
        keep = max(0, width // cell - 1)
        return text[:keep] + "~" if keep else ""

    def text(self, box: Box, value: str, font: Font, color: Color, *,
             align: str = "left", valign: str = "center") -> None:
        left, top, right, bottom = box
        value = self.fit(value, font, max(0, right - left))
        if not value:
            return
        width = font.width(value)
        if align == "right":
            x = right - width
        elif align == "center":
            x = left + (right - left - width) // 2
        else:
            x = left
        if valign == "top":
            y = top
        elif valign == "bottom":
            y = bottom - font.height
        else:
            y = top + (bottom - top - font.height) // 2
        if font.bold:
            self.canvas.text_shadow(x, y, value, color, scale=font.scale)
        else:
            self.canvas.text(x, y, value, color, scale=font.scale)


# ── the desktop, in pixels ───────────────────────────────────────────────────


class DesktopRenderer:
    """Tango-themed pixel layout over the same State the text mode draws."""

    def __init__(self, *, canvas_factory: Any | None = None) -> None:
        if canvas_factory is None:
            module, library = _soft_raster_backend()
            self._canvas = lambda w, h: module.Canvas(w, h, library=library)
        else:
            self._canvas = canvas_factory
        # (kind, index, box) hit targets from the last frame, for the mouse.
        self.hits: list[tuple[str, int, Box]] = []

    # Geometry is derived from one scale factor so the layout breathes with
    # the pane instead of truncating: fonts step down to 1x, the sidebar
    # narrows, and below a floor the sidebar folds away entirely.

    def render(self, state: State, columns: int, rows: int,
               pixel_size: tuple[int, int], *,
               clock: str | None = None) -> GraphicsFrame:
        width, height = pixel_size
        canvas = self._canvas(width, height)
        self.hits = []
        try:
            draw = Draw(canvas)
            scale = max(0.72, min(1.6, width / 1150.0, height / 640.0))
            self._background(draw, width, height)
            header_h = max(40, int(58 * scale))
            footer_h = max(18, int(24 * scale))
            self._header(draw, width, header_h, state, scale,
                         clock or time.strftime("%H:%M"))
            sidebar_w = int(210 * scale) if width >= 560 else 0
            margin = max(8, int(14 * scale))
            body_top = header_h + margin
            body_bottom = height - footer_h - margin // 2
            if sidebar_w:
                self._sidebar(draw, (margin, body_top,
                                     margin + sidebar_w, body_bottom),
                              state, scale)
                content_left = margin + sidebar_w + margin
            else:
                content_left = margin
            content = (content_left, body_top, width - margin, body_bottom)
            if state.confirm is not None:
                self._content_frame(draw, content, state, scale)
                self._confirm(draw, (0, 0, width, height), state, scale)
            else:
                self._content_frame(draw, content, state, scale)
            self._footer(draw, width, height, footer_h, state, scale)
            rgb = canvas.rgb_bytes()
        finally:
            canvas.close()
        return GraphicsFrame(rgb=rgb, width=width, height=height,
                             columns=max(1, columns), rows=max(1, rows))

    @staticmethod
    def _background(draw: Draw, width: int, height: int) -> None:
        top, bottom = tango.BG_TOP, tango.BG_BOTTOM
        steps = 24
        band = max(1, height // steps + 1)
        for index in range(steps + 1):
            y = index * band
            ratio = index / steps
            color = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio)
                          for i in range(3))
            draw.fill((0, y, width, min(height, y + band)), color)

    def _header(self, draw: Draw, width: int, header_h: int, state: State,
                scale: float, clock: str) -> None:
        draw.fill((0, 0, width, header_h), tango.HEADER)
        draw.fill((0, header_h - max(2, int(3 * scale)), width, header_h),
                  tango.BLUE)
        title = font_for(44 * scale, bold=True)
        small = font_for(11 * scale)
        pad = max(10, int(18 * scale))
        draw.text((pad, 0, width // 2, header_h - 4), "KILIX TUI", title,
                  tango.WHITE)
        release = next((v for k, v in state.status if k == "release"), "")
        strap = f"PLEBIAN-OS {release}".strip() + "  //  TEXT DESKTOP"
        draw.text((pad + title.width("KILIX TUI") + pad, 0,
                   width - pad - small.width(clock) - pad, header_h - 4),
                  strap, small, tango.GREY)
        clock_font = font_for(26 * scale, bold=True)
        draw.text((width // 2, 0, width - pad, header_h - 4), clock,
                  clock_font, tango.SILVER, align="right")

    def _sidebar(self, draw: Draw, box: Box, state: State,
                 scale: float) -> None:
        left, top, right, bottom = box
        label = font_for(24 * scale, bold=True)
        index_font = font_for(10 * scale)
        row_h = max(28, min(int(52 * scale),
                            (bottom - top) // max(1, len(SECTIONS))))
        focused = state.focus == "sections"
        for index, name in enumerate(SECTIONS):
            row_top = top + index * row_h
            row = (left, row_top + 2, right, row_top + row_h - 4)
            active = index == state.section
            danger = name == "Power"
            self.hits.append(("section", index, row))
            if active:
                # The focused column glows; the unfocused one keeps a deep
                # fill, so the eye always knows which list the arrows drive.
                if focused:
                    fill = tango.RED if danger else tango.BLUE
                else:
                    fill = tango.RED_DEEP if danger else tango.BLUE_DEEP
                draw.rounded(row, max(4, int(9 * scale)), fill)
                draw.fill((left, row[1] + 3, left + max(3, int(4 * scale)),
                           row[3] - 3),
                          tango.RED_BRIGHT if danger else tango.BLUE_BRIGHT)
                color = tango.WHITE
            else:
                color = tango.RED_BRIGHT if danger else tango.SILVER
            pad = max(10, int(16 * scale))
            draw.text((row[0] + pad, row[1], row[2] - pad, row[3]),
                      name, label, color)
            draw.text((row[0] + pad, row[1], row[2] - pad, row[3]),
                      str(index + 1), index_font,
                      tango.WHITE if active else tango.GREY_DARK,
                      align="right")

    def _content_frame(self, draw: Draw, box: Box, state: State,
                       scale: float) -> None:
        draw.panel(box, radius=max(6, int(14 * scale)))
        left, top, right, bottom = box
        pad = max(10, int(20 * scale))
        head = font_for(12 * scale, bold=True)
        name = SECTIONS[state.section]
        accent = tango.RED if name == "Power" else tango.BLUE_BRIGHT
        draw.text((left + pad, top + pad // 2, right - pad,
                   top + pad // 2 + head.height + 4),
                  state.breadcrumb().upper(), head, accent)
        if state.submenu:
            draw.text((left + pad, top + pad // 2, right - pad,
                       top + pad // 2 + head.height + 4),
                      "< back", head, tango.GREY, align="right")
        rule_y = top + pad // 2 + head.height + max(6, int(8 * scale))
        draw.hline(left + pad, right - pad, rule_y, tango.CARD_EDGE,
                   max(1, int(scale)))
        inner = (left + pad, rule_y + max(6, int(10 * scale)),
                 right - pad, bottom - pad)
        if name == "Home":
            self._home(draw, inner, state, scale)
        else:
            self._entries(draw, inner, state, scale)
        if state.message:
            note = font_for(11 * scale)
            draw.text((left + pad, bottom - pad - note.height, right - pad,
                       bottom - pad // 2), state.message, note,
                      tango.BLUE_BRIGHT, valign="bottom")

    def _home(self, draw: Draw, box: Box, state: State,
              scale: float) -> None:
        left, top, right, bottom = box
        label = font_for(10 * scale, bold=True)
        value = font_for(24 * scale, bold=True)
        detail = font_for(12 * scale)
        rows = state.status
        headline = [(k, v) for k, v in rows
                    if k in ("release", "provider", "uptime")]
        versions = [(k, v) for k, v in rows if (k, v) not in headline]
        gap = max(8, int(14 * scale))
        card_w = (right - left - gap * 2) // 3
        card_h = max(44, int(76 * scale))
        for index, (key, val) in enumerate(headline[:3]):
            x = left + index * (card_w + gap)
            card = (x, top, x + card_w, top + card_h)
            draw.rounded(card, max(4, int(10 * scale)), tango.ROW_ALT)
            draw.fill((x, card[3] - max(2, int(3 * scale)), x + card_w,
                       card[3]), tango.BLUE)
            pad = max(8, int(14 * scale))
            draw.text((x + pad, card[1] + pad // 2, card[2] - pad,
                       card[1] + pad // 2 + label.height), key.upper(),
                      label, tango.GREY, valign="top")
            draw.text((x + pad, card[1], card[2] - pad, card[3] - pad // 2),
                      val or "-", value, tango.WHITE, valign="bottom")
        y = top + card_h + gap
        row_h = max(16, int(26 * scale))
        for key, val in versions:
            if y + row_h > bottom:
                break
            draw.text((left + 4, y, left + (right - left) // 2, y + row_h),
                      key, detail, tango.SILVER)
            draw.text((left + (right - left) // 2, y, right - 4, y + row_h),
                      val, detail, tango.GREY, align="right")
            draw.hline(left + 2, right - 2, y + row_h - 1,
                       tango.ROW_ALT)
            y += row_h

    def _entries(self, draw: Draw, box: Box, state: State,
                 scale: float) -> None:
        left, top, right, bottom = box
        entries = state.entries()
        if not entries:
            font = font_for(14 * scale)
            draw.text(box, "nothing here", font, tango.GREY, align="center")
            return
        state.selected = max(0, min(state.selected, len(entries) - 1))
        label = font_for(26 * scale, bold=False)
        hint_font = font_for(11 * scale)
        focused = state.focus == "entries"
        row_h = max(26, min(int(46 * scale),
                            (bottom - top) // max(1, min(len(entries), 12))))
        capacity = max(1, (bottom - top) // row_h)
        offset = visible_window(len(entries), capacity, state.selected)
        for line in range(min(capacity, len(entries) - offset)):
            index = offset + line
            entry = entries[index]
            row_top = top + line * row_h
            row = (left, row_top + 2, right, row_top + row_h - 3)
            self.hits.append(("entry", index, row))
            selected = index == state.selected
            danger = entry.confirm
            if selected:
                if focused:
                    fill = tango.RED_DEEP if danger else tango.BLUE
                else:
                    fill = tango.ROW_ALT
                draw.rounded(row, max(4, int(8 * scale)), fill)
            pad = max(10, int(16 * scale))
            hint = entry_hint(entry)
            if entry.argv is None and not entry.submenu:
                color = tango.GREY_DARK if not selected else tango.SILVER
                hint_color = tango.GREY_DARK
            else:
                color = tango.WHITE if selected else tango.SILVER
                if entry.hint == "on" or entry.submenu or entry.verb == "tab":
                    hint_color = tango.BLUE_BRIGHT
                elif entry.confirm:
                    hint_color = tango.RED_BRIGHT
                else:
                    hint_color = tango.GREY
            if selected and focused:
                hint_color = tango.WHITE
                marker_w = max(3, int(4 * scale))
                draw.fill((row[0] + 3, row[1] + 3, row[0] + 3 + marker_w,
                           row[3] - 3),
                          tango.RED_BRIGHT if danger else tango.BLUE_BRIGHT)
            draw.text((row[0] + pad, row[1], row[2] - pad, row[3]),
                      entry.label, label, color)
            if hint:
                draw.text((row[0] + pad, row[1], row[2] - pad, row[3]),
                          hint, hint_font, hint_color, align="right")
        if len(entries) > capacity:
            more = font_for(10 * scale)
            draw.text((left, bottom - more.height, right, bottom),
                      f"{offset + 1}-{offset + capacity} / {len(entries)}",
                      more, tango.GREY_DARK, align="right")

    def _confirm(self, draw: Draw, screen: Box, state: State,
                 scale: float) -> None:
        assert state.confirm is not None
        label, argv = state.confirm
        left, top, right, bottom = screen
        draw.fill(screen, (10, 11, 13), 0.72)
        width = min(right - left - 40, max(360, int((right - left) * 0.55)))
        height = max(120, int(150 * scale))
        x = (right - left - width) // 2
        y = (bottom - top - height) // 2
        card = (x, y, x + width, y + height)
        draw.panel(card, radius=max(8, int(14 * scale)), edge=tango.RED)
        pad = max(12, int(20 * scale))
        title = font_for(26 * scale, bold=True)
        body = font_for(12 * scale)
        draw.text((x + pad, y + pad, card[2] - pad, y + pad + title.height),
                  f"Confirm: {label}", title, tango.WHITE, valign="top")
        if argv:
            draw.text((x + pad, y + pad + title.height + 8, card[2] - pad,
                       y + pad + title.height + 8 + body.height),
                      "$ " + " ".join(argv), body, tango.SILVER, valign="top")
        draw.text((x + pad, card[3] - pad - body.height, card[2] - pad,
                   card[3] - pad), "y confirm  ·  any other key cancels",
                  body, tango.RED_BRIGHT, valign="bottom")

    def _footer(self, draw: Draw, width: int, height: int, footer_h: int,
                state: State, scale: float) -> None:
        font = font_for(10 * scale)
        pad = max(10, int(18 * scale))
        top = height - footer_h
        draw.hline(0, width, top, tango.HEADER, footer_h)
        hints = footer_text(state)
        draw.text((pad, top, width - pad, height), hints, font, tango.GREY)
        # The brand tag is decoration, so it is what gives way when the two
        # will not both fit.
        if font.width(hints) + font.width("PLEBIAN-OS") + pad * 3 <= width:
            draw.text((pad, top, width - pad, height), "PLEBIAN-OS", font,
                      tango.GREY_DARK, align="right")
