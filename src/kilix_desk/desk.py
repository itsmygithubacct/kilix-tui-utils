"""The desktop itself: sections across the top, entries in the body.

Three verbs and one rule. An entry is drawn here, handed the terminal in
place, or opened in a Kilix page — and in-place is the floor: every feature
must be reachable through it, because the page verb exists only inside Kilix.
`kitty_rc.available()` is checked per action, never cached across a screen,
so the same binary serves a Kilix pane, an `ssh` session, and a bare console,
with only the page affordances appearing or disappearing.

This module is the text rendering — Tango-coloured curses, the floor every
terminal can carry. The pixel rendering over the same `State` lives in
`graphics.py`, and `main.py` picks per session.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from kilix_tui import keys as keymap, kitty_rc, privileged

from . import facts, registry, tango

SECTIONS = ("Home", "Programs", "Machine", "System", "Session", "Power")
QUIT_SENTINEL: tuple[str, ...] = ()


@dataclass(frozen=True)
class Entry:
    label: str
    argv: tuple[str, ...] | None     # None: disabled, `reason` says why
    verb: str = "inplace"
    confirm: bool = False
    reason: str = ""


def _attached_run(argv: Sequence[str]) -> int:
    """Hand the real terminal to a child and take it back afterwards.

    `endwin` drops raw mode; the event loop's next erase/render/refresh
    re-enters it, so nothing here needs to restore anything. The child's
    terminal manners are its own business — the full repaint on return is
    what makes them survivable.
    """
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


class State:
    def __init__(self, *, runner: Callable[[Sequence[str]], int] | None = None,
                 live: Callable[[], bool] | None = None) -> None:
        self.section = 0
        self.selected = 0
        self.message = ""
        self.confirm: tuple[str, tuple[str, ...]] | None = None
        self.runner = runner or _attached_run
        self.live = live or kitty_rc.available
        self.status = facts.status_rows()

    def entries(self) -> list[Entry]:
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
            plan = registry.resolve(item)
            if plan is None:
                out.append(Entry(item.label, None,
                                 reason=registry.disabled_reason(item)))
                continue
            verb = plan.verb if (plan.verb == "inplace" or live) else "inplace"
            out.append(Entry(item.label, plan.argv, verb=verb))
        return out


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
    for index, entry in enumerate(entries):
        if index >= height:
            break
        selected = index == state.selected
        if entry.argv is None:
            hint, hint_attr = entry.reason, tango.attr("muted")
        elif entry.verb == "tab":
            hint, hint_attr = "opens a page", tango.attr("accent")
        elif entry.confirm:
            hint, hint_attr = "confirms", tango.attr("alert")
        else:
            hint, hint_attr = "", 0
        marker = "▶" if selected else " "
        body = f" {marker} {entry.label}"
        pad = width - len(body) - len(hint) - 2
        if pad < 1:
            body = body[: max(0, width - len(hint) - 3)]
            pad = 1
        if selected:
            attr = tango.attr("danger" if entry.confirm else "selected")
            _put(surface, top + index, 0,
                 f"{body}{' ' * pad}{hint} ".ljust(width - 1)[: width - 1],
                 attr)
        else:
            if entry.argv is None:
                body_attr = tango.attr("muted")
            else:
                body_attr = 0
            _put(surface, top + index, 0, body[: width - 1], body_attr)
            if hint:
                _put(surface, top + index, max(0, width - len(hint) - 2),
                     hint, hint_attr)


def footer(state: State) -> str:
    if state.confirm is not None:
        return "y confirm · any other key cancels"
    return "1-6 section · Tab next · ↑/↓ · Enter open · r refresh · q quit"


def render(surface, state: State) -> None:
    height, width = surface.getmaxyx()
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
        if active:
            attr = tango.attr("danger" if name == "Power"
                              else "selected")
        elif name == "Power":
            attr = tango.attr("alert")
        else:
            attr = tango.attr("muted")
        _put(surface, 1, col, text, attr)
        col += len(text)
    _put(surface, 2, 0, "─" * max(0, width - 1), tango.attr("muted"))

    top = 3
    body_height = max(0, height - top - 2)
    if state.confirm is not None:
        label, argv = state.confirm
        _put(surface, top, 2, f"Confirm: {label}"[: width - 3],
             tango.attr("danger"))
        if argv:
            _put(surface, top + 1, 2, f"$ {' '.join(argv)}"[: width - 3],
                 tango.attr("muted"))
    elif SECTIONS[state.section] == "Home":
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
    if entry.argv is None:
        state.message = entry.reason
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
    if keymap.is_quit(key):
        # When the desktop is the whole session, quitting means a respawn or a
        # black screen, so it asks first. In a pane or over ssh it just quits.
        if os.environ.get("KILIX_TUI_SESSION") == "1":
            state.confirm = ("Leave this session", QUIT_SENTINEL)
            return True
        return False
    if ord("1") <= key <= ord("0") + len(SECTIONS):
        state.section = key - ord("1")
        state.selected = 0
        state.message = ""
        return True
    if key == ord("\t"):
        state.section = (state.section + 1) % len(SECTIONS)
        state.selected = 0
        state.message = ""
        return True
    if key in keymap.LEFT:
        state.section = (state.section - 1) % len(SECTIONS)
        state.selected = 0
        return True
    if key in keymap.RIGHT:
        state.section = (state.section + 1) % len(SECTIONS)
        state.selected = 0
        return True
    if (step := keymap.direction(key)):
        count = len(state.entries())
        if count:
            state.selected = max(0, min(count - 1, state.selected + step))
        return True
    if key in keymap.SELECT:
        entries = state.entries()
        if entries and state.selected < len(entries):
            _open(state, entries[state.selected])
        return True
    if keymap.is_refresh(key):
        state.status = facts.status_rows()
        state.message = ""
        return True
    return True
