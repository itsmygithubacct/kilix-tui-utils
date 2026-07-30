"""kilix-switch — go to any page or pane, by what it is actually doing.

The terminal already had two choosers and they were the same thing twice: a
numbered list of titles, in an overlay, one for pages and one for panes. A title
is a poor handle on a pane — several are `bash`, several more are whatever
directory they started in — so the list told you least exactly when you had
enough windows to need it.

This replaces both. One tree of pages and their panes, the process and working
directory that actually identify a pane, a filter across all of it, and a live
look at what the highlighted pane is showing. Picking is the common case and
stays one keystroke; renaming and closing are here because a chooser that can
see everything and change nothing sends you somewhere else to finish the job.

Everything it does goes through the terminal's own remote control under the
scoped credential Kilix hands each pane, so this tool can do exactly what the
pane it runs in was already permitted to do, and nothing more.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_desk import tango  # noqa: E402
from kilix_tui import app, keys as keymap, kitty_rc, shell  # noqa: E402

# The spine doubles as the scope control: which slice of the terminal is on
# screen. F12 opens on everything; the tmux-leader `q` opens on this page,
# which is the pane picker it replaces.
SCOPES = ("all", "page", "other")
SCOPE_LABELS = ("All", "This page", "Elsewhere")
PREVIEW_LINES = 14


@dataclass
class Row:
    kind: str                      # "page" | "pane"
    page: kitty_rc.Page
    pane: kitty_rc.Pane | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.kind, self.pane.id if self.pane else self.page.id)


@dataclass
class State:
    tree: kitty_rc.Tree = field(default_factory=kitty_rc.Tree)
    scope: int = 0
    cursor: int = 0
    offset: int = 0
    filter: str = ""
    mode: str = "browse"           # browse | filter | rename | confirm
    entry: str = ""                # the rename buffer
    message: str = ""
    collapsed: set[int] = field(default_factory=set)
    preview: dict[int, str] = field(default_factory=dict)
    preview_on: bool = True
    live: bool = False

    # Construction is deliberately pure — `main()` calls `refresh()`. A State
    # that loaded on construction would make every test that renders one shell
    # out to a live terminal, which is neither fast nor deterministic.

    # ── data ─────────────────────────────────────────────────────────────────

    @property
    def scope_name(self) -> str:
        return SCOPES[self.scope % len(SCOPES)]

    def refresh(self) -> None:
        """Reload the tree. Never raises — the message carries the failure."""
        self.preview.clear()
        if not kitty_rc.available():
            self.live = False
            self.tree = kitty_rc.Tree()
            self.message = "not running inside a Kilix terminal"
            return
        try:
            self.tree = kitty_rc.tree()
            self.live = True
            self.message = ""
        except kitty_rc.Unavailable as error:
            self.live = False
            self.tree = kitty_rc.Tree()
            self.message = str(error)

    def rows(self) -> list[Row]:
        active = self.tree.home_page()
        scope = self.scope_name
        out: list[Row] = []
        for page in self.tree.pages:
            if scope == "page" and (active is None or page.id != active.id):
                continue
            if scope == "other" and active is not None and page.id == active.id:
                continue
            panes = [pane for pane in page.panes if pane.matches(self.filter)]
            if self.filter and not panes and not page.matches(self.filter):
                continue
            out.append(Row("page", page))
            if page.id not in self.collapsed:
                out.extend(Row("pane", page, pane) for pane in panes)
        return out

    def current(self) -> Row | None:
        rows = self.rows()
        if not rows:
            return None
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        return rows[self.cursor]

    def preview_text(self) -> str:
        """Cached screen text for the highlighted pane.

        Fetched on demand and remembered per pane, so moving back and forth
        through the list does not ask the terminal the same question twice.
        """
        row = self.current()
        pane = row.pane if row else None
        if row and row.kind == "page":
            pane = next((p for p in row.page.panes if p.is_focused), None)
            if pane is None and row.page.panes:
                pane = row.page.panes[0]
        if pane is None or not self.live:
            return ""
        if pane.id not in self.preview:
            try:
                self.preview[pane.id] = kitty_rc.pane_text(
                    pane.id, lines=PREVIEW_LINES)
            except kitty_rc.Unavailable as error:
                self.preview[pane.id] = f"(cannot read this pane: {error})"
        return self.preview[pane.id]


# ── drawing ──────────────────────────────────────────────────────────────────


def _put(surface, row: int, col: int, text: str, attr: int = 0) -> None:
    """The same clipped write primitive used by the main desktop renderer."""
    try:
        height, width = surface.getmaxyx()
    except Exception:
        height, width = 24, 80
    if not (0 <= row < height) or col >= width:
        return
    if col < 0:
        text = text[-col:]
        col = 0
    text = text[: max(0, width - col - (1 if row == height - 1 else 0))]
    if not text:
        return
    try:
        surface.addstr(row, col, text, attr)
    except Exception:
        pass


def _row_text(row: Row, width: int, collapsed: bool = False) -> tuple[str, str]:
    """(left, right) text for a row, before any styling."""
    if row.kind == "page":
        mark = " " if not row.page.panes else ("▸" if collapsed else "▾")
        left = f"{mark} {row.page.index}  {row.page.title or '(untitled)'}"
        count = len(row.page.panes)
        return left, f"{count} pane{'' if count == 1 else 's'}"
    pane = row.pane
    assert pane is not None
    focus = "●" if pane.is_focused else " "
    left = f"    {focus} {pane.label}"
    # Below this the path is elided so hard it stops identifying anything, and
    # the process name alone is the more useful of the two.
    if width < 34:
        return left, ""
    return left, _short_path(pane.cwd, min(24, max(10, width * 2 // 5)))


def _short_path(path: str, budget: int) -> str:
    """`~`-relative and elided from the left, because the tail identifies it.

    Two directories called `src` are told apart by what is above them, but the
    thing you recognise first is the last component, so the front is what gets
    dropped.
    """
    if not path or budget <= 0:
        return ""
    home = os.path.expanduser("~")
    if path == home:
        path = "~"
    elif path.startswith(home + os.sep):
        path = "~" + path[len(home):]
    if len(path) <= budget:
        return path
    # Drop leading components until the tail fits, keeping one ellipsis.
    parts = [part for part in path.split(os.sep) if part]
    while len(parts) > 1:
        parts.pop(0)
        candidate = "…/" + os.sep.join(parts)
        if len(candidate) <= budget:
            return candidate
    return "…" + path[-(budget - 1):] if budget > 1 else "…"


def _collapse_state(state: State, height: int) -> None:
    """Keep the cursor on screen."""
    rows = state.rows()
    state.cursor = max(0, min(state.cursor, max(0, len(rows) - 1)))
    if state.cursor < state.offset:
        state.offset = state.cursor
    elif state.cursor >= state.offset + height:
        state.offset = state.cursor - height + 1
    state.offset = max(0, min(state.offset, max(0, len(rows) - height)))


def draw_list(
    surface, state: State, top: int, left: int, height: int, width: int,
) -> None:
    if height <= 0 or width <= 0:
        return
    rows = state.rows()
    _collapse_state(state, height)
    if not rows:
        note = state.message or (
            f"nothing matches “{state.filter}”" if state.filter else "no panes")
        _put(surface, top, left, note[:width], tango.attr("alert"))
        return

    for line in range(height):
        index = state.offset + line
        if index >= len(rows):
            break
        row = rows[index]
        selected = index == state.cursor
        text_left, text_right = _row_text(
            row, width, collapsed=row.page.id in state.collapsed)
        marker = "▶" if selected else " "
        body = f"{marker}{text_left}"
        pad = width - len(body) - len(text_right) - 1
        if pad < 1:
            body = body[: max(0, width - len(text_right) - 2)]
            pad = 1
        line_text = f"{body}{' ' * pad}{text_right} "[:width]

        if selected:
            _put(
                surface, top + line, left, line_text.ljust(width),
                tango.attr("selected"),
            )
        else:
            _put(
                surface, top + line, left, line_text,
                tango.attr("accent") if row.kind == "page" else 0,
            )


def draw_preview(
    surface, state: State, top: int, left: int, height: int, width: int,
) -> None:
    if height <= 2 or width <= 6:
        return
    _put(surface, top, left, "SCREEN", tango.attr("title"))
    text = state.preview_text()
    if not text:
        _put(
            surface, top + 2, left, "(no preview)"[:width],
            tango.attr("muted"),
        )
        return
    body = text.splitlines()[-(height - 2):]
    for line, content in enumerate(body):
        _put(surface, top + 2 + line, left, content[:width])


def footer(state: State) -> str:
    if state.mode == "filter":
        return f"filter: {state.filter}▏ · Enter keep · Esc clear"
    if state.mode == "rename":
        return f"rename: {state.entry}▏ · Enter apply · Esc cancel"
    if state.mode == "confirm":
        return "close this? · y confirm · any other key cancels"
    return "Enter go · / filter · Tab scope · F2 rename · x close · r reload · q quit"


def render(surface, state: State) -> None:
    try:
        surface_height, surface_width = surface.getmaxyx()
    except Exception:
        surface_height, surface_width = 24, 80
    if surface_height <= 0 or surface_width <= 0:
        return
    counts = (
        f"{len(state.tree.pages)} page{'' if len(state.tree.pages) == 1 else 's'}"
        f" · {len(state.tree.panes)} pane{'' if len(state.tree.panes) == 1 else 's'}"
    )
    if state.filter:
        counts += f" · filter “{state.filter}”"
    status = state.message or counts
    body = shell.draw(
        surface,
        title="Switcher",
        sections=SCOPE_LABELS,
        active=state.scope,
        summary=status,
        footer=footer(state),
        summary_role="alert" if state.message else "muted",
    )

    top, left = body.top, body.left
    height, width = body.height, body.width
    if height <= 0 or width <= 0:
        return
    list_width, gap = width, 0
    if state.preview_on and width >= 64:
        list_width = max(30, width * 55 // 100)
        gap = 2
    draw_list(surface, state, top, left, height, list_width)
    preview_width = width - list_width - gap
    if gap and preview_width > 6:
        separator = left + list_width
        for row in range(top, top + height):
            _put(surface, row, separator, "│", tango.attr("muted"))
        draw_preview(
            surface, state, top, separator + gap, height, preview_width)


# ── input ────────────────────────────────────────────────────────────────────


def _go(state: State) -> bool:
    """Focus what is under the cursor and leave. False ends the loop."""
    row = state.current()
    if row is None or not state.live:
        return True
    try:
        if row.kind == "pane" and row.pane is not None:
            kitty_rc.focus_pane(row.pane.id)
        else:
            kitty_rc.focus_page(row.page.id)
    except kitty_rc.Unavailable as error:
        state.message = str(error)
        return True
    return False


def _handle_filter(key: int, state: State) -> bool:
    if key in (27,):                                  # Esc
        state.filter, state.mode = "", "browse"
    elif key in (ord("\n"), ord("\r")):
        state.mode = "browse"
    elif key in (263, 127, 8):                        # Backspace
        state.filter = state.filter[:-1]
    elif 32 <= key < 127:
        state.filter += chr(key)
    state.cursor = 0
    return True


def _handle_rename(key: int, state: State) -> bool:
    if key == 27:
        state.mode, state.entry = "browse", ""
    elif key in (ord("\n"), ord("\r")):
        row = state.current()
        if row is not None and state.live:
            try:
                kitty_rc.rename_page(row.page.id, state.entry)
                state.refresh()
            except kitty_rc.Unavailable as error:
                state.message = f"rename refused: {error}"
        state.mode, state.entry = "browse", ""
    elif key in (263, 127, 8):
        state.entry = state.entry[:-1]
    elif 32 <= key < 127:
        state.entry += chr(key)
    return True


def _handle_confirm(key: int, state: State) -> bool:
    if key in (ord("y"), ord("Y")):
        row = state.current()
        if row is not None and state.live:
            try:
                if row.kind == "pane" and row.pane is not None:
                    kitty_rc.close_pane(row.pane.id)
                else:
                    kitty_rc.close_page(row.page.id)
                state.refresh()
            except kitty_rc.Unavailable as error:
                state.message = f"close refused: {error}"
    state.mode = "browse"
    return True


def handle(key: int, state: State) -> bool:
    if state.mode == "filter":
        return _handle_filter(key, state)
    if state.mode == "rename":
        return _handle_rename(key, state)
    if state.mode == "confirm":
        return _handle_confirm(key, state)

    if keymap.is_quit(key):
        return False
    if key in keymap.SELECT:
        return _go(state)
    if (step := keymap.direction(key)):
        # Clamped here rather than at render time: a cursor allowed to run past
        # the ends would need as many keypresses to come back as it took to
        # leave, which feels like the list has stopped responding.
        state.cursor = max(0, min(state.cursor + step, max(0, len(state.rows()) - 1)))
        return True
    if key in keymap.LEFT:
        row = state.current()
        if row is not None:
            state.collapsed.add(row.page.id)
        return True
    if key in keymap.RIGHT:
        row = state.current()
        if row is not None:
            state.collapsed.discard(row.page.id)
        return True
    if key == ord("/"):
        state.mode = "filter"
        return True
    if key == ord("\t"):
        state.scope = (state.scope + 1) % len(SCOPES)
        state.cursor = 0
        return True
    if key in (266, ord("R")):                        # F2, R
        row = state.current()
        state.entry = row.page.title if row else ""
        state.mode = "rename"
        return True
    if key == ord("x"):
        state.mode = "confirm"
        return True
    if key == ord("p"):
        state.preview_on = not state.preview_on
        return True
    if keymap.is_refresh(key):
        state.refresh()
        return True
    if key in keymap.HOME:
        state.cursor = 0
        return True
    if key in keymap.END:
        state.cursor = max(0, len(state.rows()) - 1)
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    state.refresh()
    if "--scope" in argv:
        index = argv.index("--scope")
        if index + 1 < len(argv) and argv[index + 1] in SCOPES:
            state.scope = SCOPES.index(argv[index + 1])
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle_:
            handle_.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
