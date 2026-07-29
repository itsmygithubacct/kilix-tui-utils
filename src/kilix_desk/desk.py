"""The desktop itself: sections in the spine, entries in the well.

Three verbs and one rule. An entry is drawn here, handed the terminal in
place, or opened in a Kilix page — and in-place is the floor: every feature
must be reachable through it, because the page verb exists only inside Kilix.
`kitty_rc.available()` is checked per action, never cached across a screen,
so the same binary serves a Kilix pane, an `ssh` session, and a bare console,
with only the page affordances appearing or disappearing.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from kilix_tui import chrome, keys as keymap, kitty_rc, panel, privileged, theme

from . import facts, registry

SECTIONS = ("Home", "Programs", "Machine", "System", "Session", "Power")
NODE = "KILIX TUI"
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


def _draw_home(surface, state: State, top: int, left: int,
               height: int, width: int) -> None:
    row = top
    for label, value in state.status:
        if row >= top + height - 1:
            break
        text = f"{label:<18}{value}"[:width]
        panel._put(surface, row, left, text,
                   theme.panel_attr("secondary", on_void=True))
        row += 1
    remaining = top + height - row - 1
    if remaining > 2 and width > 12:
        panel.readout(surface, row + 1, left, min(6, remaining), width, seed=11)


def _draw_entries(surface, state: State, top: int, left: int,
                  height: int, width: int) -> None:
    entries = state.entries()
    if not entries:
        panel._put(surface, top, left, "nothing here"[:width],
                   theme.panel_attr("alert", on_void=True))
        return
    state.selected = max(0, min(state.selected, len(entries) - 1))
    for index, entry in enumerate(entries):
        if index >= height:
            break
        selected = index == state.selected
        marker = "▶" if selected else " "
        if entry.argv is None:
            hint = entry.reason
        elif entry.verb == "tab":
            hint = "opens a page"
        elif entry.confirm:
            hint = "confirms"
        else:
            hint = ""
        body = f"{marker} {entry.label}"
        pad = width - len(body) - len(hint) - 1
        if pad < 1:
            body = body[: max(0, width - len(hint) - 2)]
            pad = 1
        text = f"{body}{' ' * pad}{hint} "[:width]
        if entry.argv is None:
            attr = theme.panel_attr("alert", on_void=True)
        elif selected:
            attr = theme.panel_attr("primary", bold=True)
            text = text.ljust(width)
        else:
            attr = theme.panel_attr("secondary", on_void=True)
        panel._put(surface, top + index, left, text, attr)


def footer(state: State) -> str:
    if state.confirm is not None:
        return "y confirm · any other key cancels"
    return "1-6 section · Tab next · ↑/↓ · Enter open · r refresh · q quit"


def render(surface, state: State) -> None:
    page = chrome.Page("Kilix TUI", SECTIONS, node=NODE)
    release = next((v for k, v in state.status if k == "release"), "")
    # The section name rides the status line because a terminal shorter than
    # ~24 rows drops the spine labels; the active section must survive that.
    status = " · ".join(part for part in (
        f"Plebian-OS {release}".rstrip(), SECTIONS[state.section]) if part)
    page.render(surface, state.section, footer=footer(state), status=status)
    top, left, height, width = page.content_box()
    if height <= 0 or width <= 0:
        return
    if state.confirm is not None:
        label, argv = state.confirm
        panel._put(surface, top, left, f"Confirm: {label}"[:width],
                   theme.panel_attr("alert", on_void=True))
        if argv:
            panel._put(surface, top + 1, left, f"$ {' '.join(argv)}"[:width],
                       theme.panel_attr("secondary", on_void=True))
        return
    if SECTIONS[state.section] == "Home":
        _draw_home(surface, state, top, left, height, width)
    else:
        _draw_entries(surface, state, top, left, height, width)
    if state.message:
        panel._put(surface, top + height - 1, left, state.message[:width],
                   theme.panel_attr("tertiary", on_void=True))


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
