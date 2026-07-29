"""The desktop itself: sections in one column, entries in the other.

Navigation is a focus model, because that is what the layout already looks
like: the section list and the entry list are two columns, Up/Down drive
whichever holds the focus, Right or Enter steps into the entries, Left steps
back out — and pops a drill-down first — while Esc walks the whole way out:
submenu, then Home, then (with a confirmation when the desktop is the whole
session) quit. Browsing sections previews their content live, since the body
always renders the current section. The mouse speaks the same vocabulary:
click a section to show it, click an entry to select it, click the selection
to open it, wheel to move.

Three verbs and one rule. An entry is drawn here, handed the terminal in
place, or opened in a Kilix page — and in-place is the floor: every feature
must be reachable through it, because the page verb exists only inside Kilix.

This module is the text rendering — Tango-coloured curses, the floor every
terminal can carry. The pixel rendering over the same `State` lives in
`graphics.py`, and `main.py` picks per session.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Sequence

from kilix_tui import keys as keymap, kitty_rc, privileged

from . import facts, registry, tango

SECTIONS = ("Home", "Programs", "Machine", "System", "Session", "Power")
QUIT_SENTINEL: tuple[str, ...] = ()
KEY_MOUSE = 409          # curses.KEY_MOUSE, named without importing curses


@dataclass(frozen=True)
class Entry:
    label: str
    argv: tuple[str, ...] | None     # None: disabled, `reason` says why
    verb: str = "inplace"
    confirm: bool = False
    reason: str = ""
    submenu: str = ""                # Enter descends instead of launching
    toggle: bool = False             # Enter flips a setting, quietly
    hint: str = ""                   # overrides the derived right-hand text


def entry_hint(entry: Entry) -> str:
    if entry.hint:
        return entry.hint
    if entry.submenu:
        return "▸"
    if entry.argv is None:
        return entry.reason
    if entry.verb == "tab":
        return "opens a page"
    if entry.confirm:
        return "confirms"
    return ""


def _attached_run(argv: Sequence[str]) -> int:
    """Hand the real terminal to a child and take it back afterwards."""
    try:
        import curses
        curses.endwin()
    except Exception:
        pass
    try:
        return subprocess.call(list(argv))
    except FileNotFoundError:
        return 127
    except OSError:
        return 126


def _quiet_run(argv: Sequence[str]) -> int:
    """Run a fast, non-interactive command without lending the terminal."""
    try:
        return subprocess.run(list(argv), capture_output=True,
                              timeout=20).returncode
    except (OSError, subprocess.SubprocessError):
        return 126


def visible_window(count: int, height: int, selected: int) -> int:
    """The first visible index, keeping the selection on screen."""
    if height <= 0 or count <= height:
        return 0
    return max(0, min(selected - height + 1, count - height))


class State:
    def __init__(self, *, runner: Callable[[Sequence[str]], int] | None = None,
                 quiet: Callable[[Sequence[str]], int] | None = None,
                 live: Callable[[], bool] | None = None) -> None:
        self.section = 0
        self.selected = 0
        self.focus = "sections"          # "sections" | "entries"
        self.submenu: str | None = None
        self.message = ""
        self.confirm: tuple[str, tuple[str, ...]] | None = None
        self.runner = runner or _attached_run
        self.quiet = quiet or _quiet_run
        self.live = live or kitty_rc.available
        self.status = facts.status_rows()
        self.text_hits: dict = {}

    # ── the data both renderers draw ─────────────────────────────────────────

    def entries(self) -> list[Entry]:
        if self.submenu == "games":
            return self._game_entries()
        if self.submenu == "screensavers":
            return self._screensaver_entries()
        name = SECTIONS[self.section]
        if name == "Home":
            return []
        if name == "Power":
            return [Entry(label, tuple(argv), confirm=True)
                    for label, argv, _needs in privileged.power_actions()]
        live = bool(self.live())
        out: list[Entry] = []
        for item in registry.SECTIONS[name]:
            if item.kilix_only and not live:
                continue
            if item.submenu:
                out.append(Entry(item.label, None, submenu=item.submenu))
                continue
            plan = registry.resolve(item)
            if plan is None:
                out.append(Entry(item.label, None,
                                 reason=registry.disabled_reason(item)))
                continue
            verb = plan.verb if (plan.verb == "inplace" or live) else "inplace"
            out.append(Entry(item.label, plan.argv, verb=verb))
        return out

    def _game_entries(self) -> list[Entry]:
        rows = registry.games()
        kilix = registry.kilix_command()
        if rows is None or kilix is None:
            return [Entry("games need a Kilix checkout", None,
                          reason="no kilix_sdk reachable")]
        out = []
        for game_id, label, enabled in rows:
            action = "disable" if enabled else "enable"
            out.append(Entry(label, (*kilix, "games", action, game_id),
                             toggle=True, hint="on" if enabled else "off"))
        return out

    def _screensaver_entries(self) -> list[Entry]:
        kilix = registry.kilix_command()
        names = registry.screensavers()
        if kilix is None or not names:
            return [Entry("screensavers need a Kilix checkout", None,
                          reason="no Kilix checkout reachable")]
        return [Entry(name, (*kilix, "screensaver", name)) for name in names]

    def breadcrumb(self) -> str:
        name = SECTIONS[self.section]
        if self.submenu:
            return f"{name} ▸ {self.submenu.capitalize()}"
        return name


# ── drawing ──────────────────────────────────────────────────────────────────


def _put(surface, row: int, col: int, text: str, attr: int = 0) -> None:
    try:
        surface.addstr(row, col, text, attr)
    except Exception:
        pass


def _draw_home(surface, state: State, top: int,
               height: int, width: int) -> None:
    row = top
    for label, value in state.status:
        if row >= top + height - 1:
            break
        _put(surface, row, 2, f"{label:<18}"[: width - 3], tango.attr("muted"))
        _put(surface, row, min(20, width - 1), str(value)[: width - 21],
             tango.attr("title"))
        row += 1


def _draw_entries(surface, state: State, top: int,
                  height: int, width: int) -> None:
    entries = state.entries()
    if not entries:
        _put(surface, top, 2, "nothing here"[: width - 3],
             tango.attr("alert"))
        return
    state.selected = max(0, min(state.selected, len(entries) - 1))
    offset = visible_window(len(entries), height, state.selected)
    state.text_hits["top"] = top
    state.text_hits["offset"] = offset
    state.text_hits["visible"] = min(height, len(entries) - offset)
    for line in range(min(height, len(entries) - offset)):
        index = offset + line
        entry = entries[index]
        selected = index == state.selected
        hint = entry_hint(entry)
        if entry.hint:
            hint_attr = tango.attr("accent" if entry.hint == "on"
                                   else "muted")
        elif entry.submenu:
            hint_attr = tango.attr("accent")
        elif entry.argv is None:
            hint_attr = tango.attr("muted")
        elif entry.verb == "tab":
            hint_attr = tango.attr("accent")
        elif entry.confirm:
            hint_attr = tango.attr("alert")
        else:
            hint_attr = 0
        marker = "▶" if selected else " "
        body = f" {marker} {entry.label}"
        pad = width - len(body) - len(hint) - 2
        if pad < 1:
            body = body[: max(0, width - len(hint) - 3)]
            pad = 1
        if selected and state.focus == "entries":
            attr = tango.attr("danger" if entry.confirm else "selected")
            _put(surface, top + line, 0,
                 f"{body}{' ' * pad}{hint} ".ljust(width - 1)[: width - 1],
                 attr)
        else:
            body_attr = tango.attr("muted") if entry.argv is None \
                and not entry.submenu else 0
            if selected:
                body_attr = tango.attr("accent")
            _put(surface, top + line, 0, body[: width - 1], body_attr)
            if hint:
                _put(surface, top + line, max(0, width - len(hint) - 2),
                     hint, hint_attr)


def footer(state: State) -> str:
    if state.confirm is not None:
        return "y confirm · any other key cancels"
    if state.submenu:
        return "↑/↓ · Enter open · ←/Esc back · q quit"
    if state.focus == "sections":
        return "↑/↓ section · →/Enter into the list · 1-6 jump · q quit"
    return "↑/↓ · Enter open · ← sections · Tab next · r refresh · q quit"


def render(surface, state: State) -> None:
    height, width = surface.getmaxyx()
    state.text_hits = {"bar_row": 1, "sections": []}
    release = next((v for k, v in state.status if k == "release"), "")
    _put(surface, 0, 1, "KILIX TUI"[: width - 2], tango.attr("title"))
    strap = f"Plebian-OS {release}".rstrip()
    _put(surface, 0, max(2, width - len(strap) - 1), strap[: width - 2],
         tango.attr("muted"))

    # The section bar carries a text marker as well as the highlight, so the
    # active section survives a monochrome terminal.
    col = 1
    for index, name in enumerate(SECTIONS):
        active = index == state.section
        marker = "▶" if active else " "
        text = f"{marker}{index + 1} {name} "
        if col + len(text) >= width:
            break
        if active and state.focus == "sections":
            attr = tango.attr("danger" if name == "Power" else "selected")
        elif active:
            attr = tango.attr("accent")
        elif name == "Power":
            attr = tango.attr("alert")
        else:
            attr = tango.attr("muted")
        _put(surface, 1, col, text, attr)
        state.text_hits["sections"].append((col, col + len(text), index))
        col += len(text)
    _put(surface, 2, 0, "─" * max(0, width - 1), tango.attr("muted"))

    top = 3
    if state.submenu:
        _put(surface, top, 1, f"◂ {state.breadcrumb()}"[: width - 2],
             tango.attr("accent"))
        top += 1
    body_height = max(0, height - top - 2)
    if state.confirm is not None:
        label, argv = state.confirm
        _put(surface, top, 2, f"Confirm: {label}"[: width - 3],
             tango.attr("danger"))
        if argv:
            _put(surface, top + 1, 2, f"$ {' '.join(argv)}"[: width - 3],
                 tango.attr("muted"))
    elif SECTIONS[state.section] == "Home" and not state.submenu:
        _draw_home(surface, state, top, body_height, width)
    else:
        _draw_entries(surface, state, top, body_height, width)

    if state.message and state.confirm is None:
        _put(surface, height - 2, 1, state.message[: width - 2],
             tango.attr("accent"))
    _put(surface, height - 1, 1, footer(state)[: width - 2],
         tango.attr("muted"))


# ── input ────────────────────────────────────────────────────────────────────


def _open(state: State, entry: Entry) -> None:
    if entry.submenu:
        state.submenu = entry.submenu
        state.selected = 0
        state.focus = "entries"
        state.message = ""
        return
    if entry.argv is None:
        state.message = entry.reason
        return
    if entry.toggle:
        code = state.quiet(entry.argv)
        state.message = (f"{entry.label}: {entry.argv[-2]}d"
                         if not code else f"{entry.label}: failed ({code})")
        return
    if entry.confirm:
        state.confirm = (entry.label, entry.argv)
        return
    if entry.verb == "tab" and state.live():
        try:
            kitty_rc.launch_tab(list(entry.argv), title=entry.label)
            state.message = f"{entry.label}: opened in a page"
            return
        except kitty_rc.Unavailable:
            pass                     # the floor: hand off in place instead
    code = state.runner(entry.argv)
    state.message = f"{entry.label} exited {code}" if code else ""


def _enter_section(state: State, section: int) -> None:
    state.section = section % len(SECTIONS)
    state.submenu = None
    state.selected = 0
    state.message = ""
    state.focus = "entries" if state.entries() else "sections"


def _quit(state: State) -> bool:
    # When the desktop is the whole session, quitting means a respawn or a
    # black screen, so it asks first. In a pane or over ssh it just quits.
    if os.environ.get("KILIX_TUI_SESSION") == "1":
        state.confirm = ("Leave this session", QUIT_SENTINEL)
        return True
    return False


def _select(state: State) -> None:
    entries = state.entries()
    if state.focus == "sections":
        if entries:
            state.focus = "entries"
            state.selected = 0
        return
    if entries and state.selected < len(entries):
        _open(state, entries[state.selected])


def _move(state: State, step: int) -> None:
    if state.focus == "sections":
        state.section = max(0, min(len(SECTIONS) - 1, state.section + step))
        state.submenu = None
        state.selected = 0
        state.message = ""
        return
    count = len(state.entries())
    if count:
        state.selected = max(0, min(count - 1, state.selected + step))


def _text_mouse(state: State) -> bool:
    """Text-mode clicks and wheel, from the layout the last render recorded."""
    try:
        import curses
        _id, x, y, _z, bstate = curses.getmouse()
    except Exception:
        return True
    hits = state.text_hits
    wheel_up = getattr_int("BUTTON4_PRESSED")
    wheel_down = getattr_int("BUTTON5_PRESSED")
    clicked = (getattr_int("BUTTON1_PRESSED") | getattr_int("BUTTON1_CLICKED")
               | getattr_int("BUTTON1_DOUBLE_CLICKED"))
    if bstate & wheel_up:
        state.focus = "entries" if state.entries() else "sections"
        _move(state, -1)
        return True
    if bstate & wheel_down:
        state.focus = "entries" if state.entries() else "sections"
        _move(state, 1)
        return True
    if not bstate & clicked:
        return True
    if y == hits.get("bar_row"):
        for start, end, index in hits.get("sections", ()):
            if start <= x < end:
                _enter_section(state, index)
                break
        return True
    top = hits.get("top")
    if top is None or not (top <= y < top + hits.get("visible", 0)):
        return True
    index = hits.get("offset", 0) + (y - top)
    if state.focus == "entries" and index == state.selected:
        entries = state.entries()
        if index < len(entries):
            _open(state, entries[index])
    else:
        state.focus = "entries"
        state.selected = index
    return True


def getattr_int(name: str) -> int:
    try:
        import curses
        return int(getattr(curses, name))
    except Exception:
        return 0


def handle(key: int, state: State) -> bool:
    if state.confirm is not None:
        label, argv = state.confirm
        state.confirm = None
        if key in (ord("y"), ord("Y")):
            if argv == QUIT_SENTINEL:
                return False
            code = state.runner(argv)
            state.message = f"{label} exited {code}" if code else f"{label}: done"
        else:
            state.message = f"cancelled: {label}"
        return True
    if key == KEY_MOUSE:
        return _text_mouse(state)
    if key == 27:
        # Esc walks out one level at a time: drill-down, then Home, then quit.
        if state.submenu:
            state.submenu = None
            state.selected = 0
            return True
        if state.section != 0 or state.focus == "entries":
            state.section = 0
            state.focus = "sections"
            state.selected = 0
            state.message = ""
            return True
        return _quit(state)
    if key in (ord("q"), ord("Q")):
        return _quit(state)
    if ord("1") <= key <= ord("0") + len(SECTIONS):
        _enter_section(state, key - ord("1"))
        return True
    if key == ord("\t"):
        _enter_section(state, state.section + 1)
        return True
    if key in keymap.LEFT:
        if state.submenu:
            state.submenu = None
            state.selected = 0
        elif state.focus == "entries":
            state.focus = "sections"
        return True
    if key in keymap.RIGHT:
        if state.focus == "sections":
            if state.entries():
                state.focus = "entries"
                state.selected = 0
        else:
            entries = state.entries()
            if entries and entries[state.selected].submenu:
                _open(state, entries[state.selected])
        return True
    if (step := keymap.direction(key)):
        _move(state, step)
        return True
    if key in keymap.PAGE_UP:
        _move(state, -5)
        return True
    if key in keymap.PAGE_DOWN:
        _move(state, 5)
        return True
    if key in keymap.HOME:
        if state.focus == "entries":
            state.selected = 0
        else:
            _move(state, -len(SECTIONS))
        return True
    if key in keymap.END:
        if state.focus == "entries":
            state.selected = max(0, len(state.entries()) - 1)
        else:
            _move(state, len(SECTIONS))
        return True
    if key in keymap.SELECT:
        _select(state)
        return True
    if keymap.is_refresh(key):
        state.status = facts.status_rows()
        state.message = ""
        return True
    return True
