"""The panel drawing vocabulary: flat colour blocks, elbows, bars, and pills.

A deliberately theatrical layout language for the tools that want one — solid
colour on black, separation by black gap rather than by border, and a lot of
decorative structure around not much content. `theme.PANEL` holds the palette
and the reasoning; this module is only the drawing.

Two rules hold the whole thing together:

**Text first, colour second.** Every primitive writes real characters and then
asks `theme.panel_attr()` for styling. On a terminal that cannot do 256
colours — or in the headless render path, where curses was never started —
the attribute is 0 and the same text lands unstyled. That is what lets these
tools be asserted on as plain text and still work over `ssh` from a 16-colour
terminal.

**Curves come from glyphs, not arithmetic.** Terminal cells are about twice as
tall as they are wide, so a radius computed in cells looks stretched. The
corners here are single quadrant glyphs (`▗▖▘▝`) laid over the corner of a
filled rectangle, which shaves it by exactly half a cell in each direction and
reads as a curve at any size.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from . import theme

# The corner glyph that shaves each corner of a filled rectangle. Drawn in the
# panel colour on the void, so the *un-drawn* part of the cell is what rounds it.
_SHAVE = {
    "tl": "▗",   # ▗ lower right  — shaves the top-left
    "tr": "▖",   # ▖ lower left   — shaves the top-right
    "bl": "▝",   # ▝ upper right  — shaves the bottom-left
    "br": "▘",   # ▘ upper left   — shaves the bottom-right
}

# Pill caps: a half block whose filled half faces into the pill.
_CAP_LEFT = "▐"    # ▐ right half block
_CAP_RIGHT = "▌"   # ▌ left half block


def _size(surface: Any) -> tuple[int, int]:
    try:
        height, width = surface.getmaxyx()
    except Exception:
        return 24, 80
    return height, width


def _put(surface: Any, row: int, col: int, text: str, attr: int) -> None:
    """Write clipped to the surface, and never let curses' last-cell raise."""
    height, width = _size(surface)
    if not (0 <= row < height) or col >= width:
        return
    if col < 0:
        text = text[-col:]
        col = 0
    text = text[: max(0, width - col)]
    if not text:
        return
    # Writing the final cell of the final row scrolls a real curses window and
    # raises; every tool here would rather lose one character than crash.
    if row == height - 1 and col + len(text) >= width:
        text = text[: width - col - 1]
        if not text:
            return
    try:
        surface.addstr(row, col, text, attr)
    except Exception:
        pass


def fill(
    surface: Any, top: int, left: int, height: int, width: int, colour: str,
) -> None:
    """A solid rectangle of `colour`, no rounding."""
    if height <= 0 or width <= 0:
        return
    attr = theme.panel_attr(colour)
    for row in range(top, top + height):
        _put(surface, row, left, " " * width, attr)


def block(
    surface: Any,
    top: int,
    left: int,
    height: int,
    width: int,
    colour: str,
    label: str | None = None,
    ident: str | None = None,
    *,
    round_corners: Sequence[str] = ("tl", "tr", "bl", "br"),
    active: bool = False,
) -> None:
    """A rounded colour block, optionally labelled and identified.

    `label` sits at the top-left in black; `ident` is the small numeric tag at
    the bottom-right that every block in the reference carries whether or not it
    means anything. `active` swaps the fill to the primary colour, which is held
    out of the normal cycle so the selected block is unmistakable.
    """
    if height <= 0 or width <= 0:
        return
    if active:
        colour = "primary"
    fill(surface, top, left, height, width, colour)

    shave = theme.panel_attr(colour, on_void=True)
    corners = {
        "tl": (top, left),
        "tr": (top, left + width - 1),
        "bl": (top + height - 1, left),
        "br": (top + height - 1, left + width - 1),
    }
    for corner in round_corners:
        if corner in corners:
            row, col = corners[corner]
            _put(surface, row, col, _SHAVE[corner], shave)

    ink = theme.panel_attr(colour, bold=True)
    if label and width > 2:
        _put(surface, top, left + 1, label[: width - 2], ink)
    if ident and width > 2 and height >= 1:
        tag = ident[: width - 2]
        _put(surface, top + height - 1, left + width - 1 - len(tag), tag, ink)


def elbow(
    surface: Any,
    top: int,
    left: int,
    height: int,
    width: int,
    colour: str,
    *,
    corner: str = "tl",
    thickness: int = 2,
    stem: int = 4,
    cap: bool = True,
) -> None:
    """The signature shape: a thick corner sweeping into a horizontal bar.

    `stem` is how wide the vertical leg is, `thickness` how tall the horizontal
    leg is. `corner` names which corner of the bounding box the mass occupies:
    "tl" is a leg down the left joined to a bar along the top.

    This is the piece that decides whether the layout reads as the reference or
    as a coloured TUI, so it gets the outer corner shaved and the inner corner
    notched — both with quadrant glyphs, never with arithmetic.
    """
    if height <= 0 or width <= 0:
        return
    stem = max(1, min(stem, width))
    thickness = max(1, min(thickness, height))
    top_side = corner.startswith("t")
    left_side = corner.endswith("l")

    bar_row = top if top_side else top + height - thickness
    leg_col = left if left_side else left + width - stem

    fill(surface, bar_row, left, thickness, width, colour)
    fill(surface, top, leg_col, height, stem, colour)

    shave = theme.panel_attr(colour, on_void=True)
    outer = ("t" if top_side else "b") + ("l" if left_side else "r")
    _put(
        surface,
        top if top_side else top + height - 1,
        left if left_side else left + width - 1,
        _SHAVE[outer],
        shave,
    )
    # The inner corner is the diagonal opposite of the outer one, one cell in
    # from where the two legs meet.
    inner_row = bar_row + thickness if top_side else bar_row - 1
    inner_col = leg_col + stem if left_side else leg_col - 1
    inner = ("b" if top_side else "t") + ("r" if left_side else "l")
    if top <= inner_row < top + height and left <= inner_col < left + width:
        _put(surface, inner_row, inner_col, _SHAVE[inner], shave)

    # The far end of the bar gets a rounded cap too, or it reads as cut off —
    # unless a segmented run is about to continue it, in which case a cap would
    # close a bar that has not ended.
    if cap:
        cap_col = left + width - 1 if left_side else left
        cap_corner = ("t" if top_side else "b") + ("r" if left_side else "l")
        _put(surface, bar_row, cap_col, _SHAVE[cap_corner], shave)


def bar(
    surface: Any,
    row: int,
    left: int,
    width: int,
    segments: Iterable[tuple[int, str]] | None = None,
) -> None:
    """A horizontal run broken into unequal coloured segments.

    Carries no information — it is rhythm, and it is most of what makes the
    layout look busy while saying nothing. `segments` is (cells, colour) pairs;
    without them the width is divided into an uneven default run so a caller
    that just wants texture does not have to invent one.
    """
    if width <= 0:
        return
    if segments is None:
        # Deliberately uneven: equal segments read as a progress bar.
        weights = (5, 2, 9, 3, 7, 4)
        total = sum(weights)
        cycle = theme.PANEL_CYCLE
        segments = [
            (max(1, round(width * weight / total)), cycle[i % len(cycle)])
            for i, weight in enumerate(weights)
        ]
    col = left
    limit = left + width
    for cells, colour in segments:
        if col >= limit:
            break
        cells = min(cells, limit - col)
        if cells <= 0:
            continue
        fill(surface, row, col, 1, cells, colour)
        col += cells + 1        # the black gap between segments


def pill(
    surface: Any,
    row: int,
    left: int,
    label: str,
    colour: str = "quaternary",
    *,
    active: bool = False,
    width: int | None = None,
) -> int:
    """A fully rounded button with dark text inside. Returns the width used."""
    if active:
        colour = "primary"
    text = f" {label} "
    if width is not None:
        text = text[:width].ljust(max(0, width))
    fill_attr = theme.panel_attr(colour, bold=True)
    cap_attr = theme.panel_attr(colour, on_void=True)
    _put(surface, row, left, _CAP_LEFT, cap_attr)
    _put(surface, row, left + 1, text, fill_attr)
    _put(surface, row, left + 1 + len(text), _CAP_RIGHT, cap_attr)
    return len(text) + 2


def title(surface: Any, row: int, right: int, text: str) -> None:
    """The heavy right-aligned title that anchors the upper right."""
    text = text.upper()
    _put(
        surface,
        row,
        max(0, right - len(text)),
        text,
        theme.panel_attr("primary", on_void=True, bold=True),
    )


def readout(
    surface: Any, top: int, left: int, rows: int, cols: int, *, seed: int = 0,
) -> None:
    """Columns of meaningless numbers, as texture.

    Deterministic from `seed` so a screenshot test is stable — the numbers are
    decoration, and decoration that changes every frame would be noise in a
    diff as well as on screen.
    """
    if rows <= 0 or cols <= 0:
        return
    attr = theme.panel_attr("secondary", on_void=True)
    value = (seed * 2654435761 + 1) & 0xFFFFFFFF
    for row in range(rows):
        cells: list[str] = []
        used = 0
        while used < cols:
            value = (value * 1103515245 + 12345) & 0x7FFFFFFF
            digits = 4 if value % 3 else 7
            cells.append(str(value % (10 ** digits)).rjust(digits, "0"))
            used += digits + 1
        _put(surface, top + row, left, " ".join(cells)[:cols], attr)


def ident(seed: int, section: int) -> str:
    """A stable `NN-NNNNNN` tag for a spine section."""
    value = (seed * 2654435761 + section * 40503) & 0x7FFFFFFF
    return f"{section:02d}-{value % 1000000:06d}"
