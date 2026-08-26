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


# One tip per tool, keyed by the title the tool already passes to `draw`.
# Keeping them here rather than in each tool means the whole suite gains tips
# from one file, and a tip earns its row only by saying something the screen
# does not already show.
TIPS: dict[str, str] = {
    "Calculator": "answers stay in the list — Up recalls what you typed",
    "CPU": "the busiest processes are listed under the per-core bars",
    "Disk": "Enter scans a filesystem; it stays interruptible while it runs",
    "Files": "this manager only opens and navigates — it never deletes or moves",
    "Launcher": "picking a row replaces this tab with the launch; ! runs a typed command",
    "Music": "this drives kilix-amp over its control socket, so the player can be anywhere",
    "Packages": "read-only: type to filter, Tab re-sorts by size",
    "Plebian-OS Control": "every power and update action confirms before it runs",
    "Rollout Resume": "resumes Claude, Codex and Kimi sessions — and installs them",
    "Session Logs": "live and archived transcripts read the same; archives decompress on the fly",
    "Switcher": "shows what each pane is running before you jump to it",
    "Pane Center": "coding-session state and pane text update without leaving this view",
    "System": "--print gives the same facts as plain text for scripts",
    "Volume": "this sets the sink Kilix itself uses, not just this pane",
    "Volume settings": "mute applies immediately; Enter opens every output",
    "Weather": "forecast comes from Open-Meteo; r refetches it",
    "Cameras": "views come from kilix-rtsp; n writes a new stream profile",
    "Character Map": "Enter copies through OSC 52, so it also works over SSH",
    "Find Files": "search is bounded and follows no directory symlinks",
    "Notepad": "UTF-8 files save atomically; Ctrl-Q warns before discarding edits",
    "Temperatures": "green below 80°C, yellow to 89°C, red above",
    "Memory": "pressure matters more than free bytes — watch the stall lines",
    "VirtualBox": "machines launch into their own Kilix tab",
}

# What the last frame drew, so the `?` overlay can explain the keys the user is
# actually looking at instead of a generic list. The curses loop is single
# threaded and draws one frame at a time, so one slot is enough.
_LAST: dict[str, str] = {"title": "", "footer": ""}


def last_frame() -> dict[str, str]:
    return dict(_LAST)


def fit(text: str, width: int, separator: str = " · ") -> str:
    """Fit a `·`-separated key line to the width without losing the way out.

    Truncating a key line silently removes the keys furthest right, and those
    are the ones that get a stuck user out. So segments drop from the middle
    outwards, and two are protected until nothing else is left to drop: the
    last one, and `?`. Quitting is the way out of the tool; `?` is the way to
    everything that did not fit — dropping it takes the map away exactly when
    the screen is too small to show the territory.
    """
    if width <= 0 or len(text) <= width:
        return text
    parts = text.split(separator)

    def droppable() -> list[int]:
        last = len(parts) - 1
        return [i for i, part in enumerate(parts)
                if i != last and "?" not in part]

    while len(parts) > 1 and len(separator.join(parts)) > width:
        candidates = droppable() or [i for i in range(len(parts) - 1)]
        if not candidates:
            break
        del parts[candidates[-1]]        # nearest the end, from the middle out
    line = separator.join(parts)
    return line if len(line) <= width else line[:width]


# Keys that are spelled as words. Without these, "space play/pause" reads as
# prose to the parser below and loses its key column in the `?` overlay.
NAMED_KEYS = frozenset({
    "space", "tab", "enter", "esc", "escape", "backspace", "del", "delete",
    "home", "end", "pgup", "pgdn", "mouse", "wheel", "shift", "ctrl", "alt",
})


def help_rows_for(footer: str) -> list[tuple[str, str]]:
    """Turn a tool's own key line into rows for the `?` overlay.

    Deriving the overlay from the footer the tool already writes means the two
    can never disagree, and no tool has to author its help twice.
    """
    rows: list[tuple[str, str]] = []
    for segment in footer.split(" · "):
        segment = segment.strip()
        if not segment:
            continue
        head, _, rest = segment.partition(" ")
        named = head.lower() in NAMED_KEYS
        # "type to filter" is a sentence, not a binding: keep it whole. But
        # "space play/pause" is a binding whose key happens to be a word, so
        # the keys that are spelled out get their own column like the rest.
        if not rest or (not named and head.isalpha() and head.islower()
                        and len(head) > 2):
            rows.append(("", segment))
        else:
            rows.append((head, rest))
    return rows


class Filter:
    """Incremental `/` filtering, defined once for every browsable list.

    A list long enough to scroll is a list worth searching, and a tool that
    invents its own search box is a tool with its own keys to learn. This owns
    the whole interaction — open on `/`, type to narrow, Enter to keep the
    needle and resume navigating, Esc to drop it — so a tool spends three
    lines: consume keys, filter its rows, hand the summary to `draw`.

    Live dashboards are the deliberate exception. A process list ranked by
    usage rearranges under the cursor every second, so a needle typed into it
    is aiming at something that has already moved.
    """

    def __init__(self) -> None:
        self.text = ""
        self.typing = False

    def active(self) -> bool:
        return bool(self.text) or self.typing

    def open(self) -> None:
        self.typing = True

    def clear(self) -> None:
        self.text = ""
        self.typing = False

    def handle(self, key: int) -> bool:
        """Consume a key while filtering. True means the tool must not act."""
        from . import keys as keymap
        if not self.typing:
            if keymap.is_filter(key):
                self.open()
                return True
            return False
        if key == keymap.ESCAPE:
            self.clear()
        elif key in keymap.ENTER:
            self.typing = False          # keep the needle, resume navigating
        elif key in keymap.BACKSPACE:
            self.text = self.text[:-1]
        elif keymap.is_text(key):
            self.text += chr(key)
        return True

    def matches(self, label: object) -> bool:
        return self.text.lower() in str(label).lower()

    def apply(self, items, key=None) -> list:
        if not self.text:
            return list(items)
        pick = key or (lambda item: item)
        return [item for item in items if self.matches(pick(item))]

    def summary(self, shown: int) -> str:
        if not self.active():
            return ""
        tail = "1 match" if shown == 1 else f"{shown} matches"
        return f"filter: {self.text}{'_' if self.typing else ''}  ({tail})"

    def footer(self) -> str:
        return "type to filter · Enter keep · Esc clear · ↑↓ move"


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
    help_key: bool = True,
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

    # Every screen that *has* `?` advertises it, and the line is fitted rather
    # than clipped. A tool where typing is text (`help_key=False`) must not
    # advertise a key that does nothing there.
    line = str(footer)
    if line and help_key and "?" not in line:
        # Inserted before the final segment, never after it. Appending pushed
        # the tool's own last binding — almost always "q quit" — out of the one
        # position `fit` protects, and the trimmer then dropped the way out of
        # the tool while keeping the help key. The end of the line is reserved
        # for quitting.
        parts = line.split(" · ")
        parts.insert(max(0, len(parts) - 1), "? keys")
        line = " · ".join(parts)
    line = fit(line, inner_width)
    put(surface, height - 1, left, line, tango.attr("muted"))

    tip = tip or TIPS.get(str(title), "")
    if tip:
        put(surface, height - 2, left, fit(f"tip  {tip}", inner_width),
            tango.attr("accent"))
    _LAST["title"] = str(title)
    _LAST["footer"] = str(footer)

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
