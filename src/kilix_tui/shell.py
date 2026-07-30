"""The canonical text shell shared by every Kilix TUI.

The VirtualBox manager established the layout:

* identity and application name on row zero;
* numbered navigation on row one, with a visible ``▶`` active marker;
* one quiet divider on row two;
* a compact status line on row three;
* application content below it and one footer on the last row.

Keeping that frame here prevents individual tools from inventing another
header, palette, or panel system.  Applications still own everything inside
the returned body rectangle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from kilix_desk import tango


@dataclass(frozen=True)
class Body:
    """The content rectangle left after the shared shell is drawn."""

    top: int
    left: int
    height: int
    width: int

    @property
    def bottom(self) -> int:
        return self.top + self.height


def put(surface, row: int, column: int, value: object, attr: int = 0) -> None:
    """Write safely into either curses or the headless text surface."""
    try:
        height, width = surface.getmaxyx()
    except Exception:
        height, width = 24, 80
    if not (0 <= row < height) or width <= 0:
        return
    column = max(0, column)
    if column >= width:
        return
    text = str(value)[:max(0, width - column)]
    if not text:
        return
    try:
        surface.addstr(row, column, text, attr)
    except Exception:
        # A real curses window can reject its bottom-right cell even when the
        # coordinates are otherwise valid.  Losing one clipped write is safer
        # than taking down the whole utility during a resize.
        pass


def draw(
    surface,
    *,
    title: str,
    sections: Sequence[str] = ("Overview",),
    active: int = 0,
    summary: str = "",
    footer: str = "",
    tab_roles: Sequence[str] | None = None,
    summary_role: str = "muted",
) -> Body:
    """Draw the shared four-row frame and return its content rectangle.

    ``tab_roles`` is intentionally role-based rather than raw curses flags.
    It lets the desktop keep Power red and a detail-focused section blue while
    every tool still draws from the one Tango palette.
    """
    try:
        height, width = surface.getmaxyx()
    except Exception:
        height, width = 24, 80
    if height <= 0 or width <= 0:
        return Body(0, 0, 0, 0)

    left = 1 if width > 2 else 0
    inner_width = max(0, width - (2 if width > 2 else 1))
    put(surface, 0, left, "KILIX TUI"[:inner_width], tango.attr("title"))

    strap = str(title)
    strap_column = width - len(strap) - 1
    if strap and strap_column > left + len("KILIX TUI"):
        put(surface, 0, strap_column, strap, tango.attr("muted"))

    labels = tuple(sections) or ("Overview",)
    column = left
    for index, label in enumerate(labels):
        marker = "▶" if index == active else " "
        text = f"{marker}{index + 1} {label} "
        if column + len(text) >= width:
            break
        role = (
            tab_roles[index]
            if tab_roles is not None and index < len(tab_roles)
            else ("selected" if index == active else "muted")
        )
        put(surface, 1, column, text, tango.attr(role))
        column += len(text)

    put(surface, 2, 0, "─" * max(0, width - 1), tango.attr("muted"))
    put(surface, 3, left, str(summary)[:inner_width], tango.attr(summary_role))
    put(surface, height - 1, left, str(footer)[:inner_width],
        tango.attr("muted"))

    top = 4
    return Body(
        top=top,
        left=left,
        height=max(0, height - top - 1),
        width=inner_width,
    )
