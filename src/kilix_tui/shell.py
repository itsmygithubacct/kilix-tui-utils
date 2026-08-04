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
    breadcrumb: str = "",
    tip: str = "",
) -> Body:
    """Draw the shared frame and return its content rectangle.

    ``tab_roles`` is intentionally role-based rather than raw curses flags.
    It lets the desktop keep Power red and a detail-focused section blue while
    every tool still draws from the one Tango palette.

    ``breadcrumb`` replaces the numbered strip with a trail of where you are —
    ncdu prints the whole path on every screen for the same reason: a list you
    cannot place is a list you cannot trust. ``tip`` reserves the row above the
    footer for one short hint. Both default to off, so a tool that has not
    adopted them draws exactly the frame it drew before.
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

    if breadcrumb:
        # "── Home › Programs › Games ──────" : the trail, ruled to the edge.
        trail = f"── {breadcrumb} "
        put(surface, 1, 0, trail[:width - 1], tango.attr("accent"))
        if len(trail) < width - 1:
            put(surface, 1, len(trail), "─" * (width - 1 - len(trail)),
                tango.attr("muted"))
    else:
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
    if tip:
        put(surface, height - 2, left, f"tip  {tip}"[:inner_width],
            tango.attr("accent"))

    top = 4
    reserved = 2 if tip else 1          # footer, plus the tip row when present
    return Body(
        top=top,
        left=left,
        height=max(0, height - top - reserved),
        width=inner_width,
    )


def meter(fraction: float, width: int, *, full: str = "█",
          empty: str = "░") -> str:
    """A proportional bar. The disk tool's bar, available to every tool."""
    if width <= 0:
        return ""
    fraction = 0.0 if fraction < 0 else (1.0 if fraction > 1 else fraction)
    filled = int(round(fraction * width))
    return full * filled + empty * (width - filled)


def overlay(surface, *, title: str, rows: Sequence[tuple[str, str]],
            note: str = "") -> None:
    """Draw a centred, bordered panel over the current screen.

    This is the `?` help ncdu shows: the keys are explained on top of the thing
    they act on, so nobody has to leave to find out how to stay.
    """
    try:
        height, width = surface.getmaxyx()
    except Exception:
        return
    if not rows or height < 6 or width < 24:
        return
    left_column = max((len(key) for key, _ in rows), default=0)
    body_width = max(
        len(title) + 4,
        max((left_column + len(text) + 6 for _, text in rows), default=0),
        len(note) + 4,
    )
    body_width = min(body_width, width - 4)
    body_height = min(len(rows) + (3 if note else 2), height - 2)
    top = max(0, (height - body_height) // 2)
    start = max(0, (width - body_width) // 2)

    heading = f"─ {title} ".ljust(body_width - 2, "─")
    put(surface, top, start, f"┌{heading}┐", tango.attr("accent"))
    for index, (key, text) in enumerate(rows[:body_height - 2]):
        line = f" {key:>{left_column}}  {text}".ljust(body_width - 2)
        put(surface, top + 1 + index, start, "│", tango.attr("accent"))
        put(surface, top + 1 + index, start + 1, line[:body_width - 2])
        put(surface, top + 1 + index, start + body_width - 1, "│",
            tango.attr("accent"))
    row = top + body_height - (2 if note else 1)
    if note:
        put(surface, row, start, "│", tango.attr("accent"))
        put(surface, row, start + 1, f" {note}".ljust(body_width - 2)[
            :body_width - 2], tango.attr("muted"))
        put(surface, row, start + body_width - 1, "│", tango.attr("accent"))
        row += 1
    put(surface, row, start, "└" + "─" * (body_width - 2) + "┘",
        tango.attr("accent"))
