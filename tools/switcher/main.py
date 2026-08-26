"""Kilix Pane Center — understand, inspect, and address every live pane.

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

The same joined state is available as ``kilix panes list/dump/wait/send`` for
agents and scripts; ``kilix-switch`` remains the F12-compatible TUI name.

Everything it does goes through the terminal's own remote control under the
scoped credential Kilix hands each pane, so this tool can do exactly what the
pane it runs in was already permitted to do, and nothing more.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_desk import tango  # noqa: E402
from kilix_tui import (  # noqa: E402
    app, keys as keymap, kitty_rc, pane_center, shell,
)

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
    snapshot: pane_center.Snapshot = field(default_factory=pane_center.Snapshot)
    inspector: pane_center.Inspector = field(default_factory=pane_center.Inspector)
    scope: int = 0
    cursor: int = 0
    offset: int = 0
    filter: str = ""
    mode: str = "browse"           # browse | filter | rename | send | confirm
    entry: str = ""                # rename or message buffer
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
            self.snapshot = self.inspector.snapshot(self.tree)
            self.live = True
            self.message = ""
        except kitty_rc.Unavailable as error:
            self.live = False
            self.tree = kitty_rc.Tree()
            self.snapshot = pane_center.Snapshot()
            self.message = str(error)

    def info(self, pane_id: int) -> pane_center.PaneInfo | None:
        return self.snapshot.by_id(pane_id)

    def rows(self) -> list[Row]:
        active = self.tree.home_page()
        scope = self.scope_name
        out: list[Row] = []
        for page in self.tree.pages:
            if scope == "page" and (active is None or page.id != active.id):
                continue
            if scope == "other" and active is not None and page.id == active.id:
                continue
            panes = [
                pane for pane in page.panes
                if not self.filter
                or (
                    (self.info(pane.id).searchable
                     if self.info(pane.id) is not None
                     else " ".join((
                         pane.title, pane.process, pane.cwd,
                         pane.page_title, " ".join(pane.argv),
                     )))
                    .casefold().find(self.filter.casefold()) >= 0
                )
            ]
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


def _row_text(
    row: Row,
    width: int,
    collapsed: bool = False,
    info: pane_center.PaneInfo | None = None,
) -> tuple[str, str]:
    """(left, right) text for a row, before any styling."""
    if row.kind == "page":
        mark = " " if not row.page.panes else ("▸" if collapsed else "▾")
        left = f"{mark} {row.page.index}  {row.page.title or '(untitled)'}"
        count = len(row.page.panes)
        return left, f"{count} pane{'' if count == 1 else 's'}"
    pane = row.pane
    assert pane is not None
    focus = "●" if pane.is_focused else " "
    state = info.activity if info is not None else ""
    badge = f"{state:<7}" if state else ""
    left = f"    {focus} {badge} {pane.label}" if badge else f"    {focus} {pane.label}"
    # Below this the path is elided so hard it stops identifying anything, and
    # the process name alone is the more useful of the two.
    if width < 34:
        return left, ""
    right = _short_path(pane.cwd, min(24, max(10, width * 2 // 5)))
    if info is not None and info.coding is not None and width >= 54:
        right = f"{info.coding.provider} · {right}" if right else info.coding.provider
    return left, right


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
            row,
            width,
            collapsed=row.page.id in state.collapsed,
            info=(state.info(row.pane.id) if row.pane is not None else None),
        )
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
    row = state.current()
    pane = row.pane if row else None
    if row is not None and pane is None:
        pane = next((item for item in row.page.panes if item.is_focused), None)
        if pane is None and row.page.panes:
            pane = row.page.panes[0]
    info = state.info(pane.id) if pane is not None else None
    _put(surface, top, left, "PANE DETAIL", tango.attr("title"))
    detail_rows: list[str] = []
    if pane is not None:
        activity = info.activity if info is not None else pane.process
        identity = f"pane {pane.id} · {activity}"
        if info is not None and info.coding is not None:
            identity += (
                f" · {info.coding.provider} {info.coding.short_id}"
                f" · {info.age or 'live'}"
            )
        detail_rows.append(identity)
        detail_rows.append(_short_path(pane.cwd, width))
        if info is not None and info.broker is not None:
            broker = info.broker
            detail_rows.append(
                f"broker {broker.id[:8]} · "
                f"{'attached' if broker.attached else 'detached'} · "
                f"journal {pane_center.format_bytes(broker.journal_bytes)}"
            )
        if info is not None and info.doing:
            detail_rows.append(f"doing: {info.doing}")
    detail_rows = detail_rows[:max(0, height - 4)]
    for line, content in enumerate(detail_rows, 1):
        _put(surface, top + line, left, content[:width],
             tango.attr("muted") if line != len(detail_rows) else 0)
    screen_top = top + len(detail_rows) + 1
    if screen_top < top + height:
        _put(surface, screen_top, left, "SCREEN", tango.attr("title"))
    text = state.preview_text()
    if not text:
        _put(
            surface, screen_top + 1, left, "(no preview)"[:width],
            tango.attr("muted"),
        )
        return
    available = max(0, top + height - screen_top - 1)
    body = text.splitlines()[-available:]
    for line, content in enumerate(body):
        _put(surface, screen_top + 1 + line, left, content[:width])


def footer(state: State) -> str:
    if state.mode == "filter":
        return f"filter: {state.filter}▏ · Enter keep · Esc clear"
    if state.mode == "rename":
        return f"rename: {state.entry}▏ · Enter apply · Esc cancel"
    if state.mode == "send":
        return f"message: {state.entry}▏ · Enter send · Esc cancel"
    if state.mode == "confirm":
        return "close this? · y confirm · any other key cancels"
    return (
        "Enter go · s message · / filter · Tab scope · p preview · "
        "F2 rename · x close · r reload · q quit"
    )


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
        help_key=False,   # '?' is text here, not help
        title="Pane Center",
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


def _selected_pane(state: State) -> kitty_rc.Pane | None:
    row = state.current()
    if row is None:
        return None
    if row.pane is not None:
        return row.pane
    pane = next((item for item in row.page.panes if item.is_focused), None)
    return pane or (row.page.panes[0] if row.page.panes else None)


def _handle_send(key: int, state: State) -> bool:
    if key == 27:
        state.mode, state.entry = "browse", ""
    elif key in (ord("\n"), ord("\r")):
        pane = _selected_pane(state)
        if pane is not None and state.live:
            try:
                size = kitty_rc.send_text(pane, state.entry + "\r")
                state.preview.pop(pane.id, None)
                state.message = f"queued {size} bytes for pane {pane.id}"
            except kitty_rc.Unavailable as error:
                state.message = f"message refused: {error}"
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
    if state.mode == "send":
        return _handle_send(key, state)
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
    if key == ord("s"):
        if _selected_pane(state) is not None:
            state.entry = ""
            state.mode = "send"
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


def tick(state: State) -> None:
    """Keep activity and the selected screen current while browsing."""
    if state.mode == "browse":
        state.refresh()


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError("must be between 1 and 10000")
    return number


def _seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not 0 <= number <= 86400:
        raise argparse.ArgumentTypeError("must be between 0 and 86400")
    return number


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kilix panes",
        description=(
            "Inspect and communicate with panes in this Kilix window. "
            "With no command, open the interactive pane center."
        ),
        epilog=(
            "examples:\n"
            "  kilix panes list --json\n"
            "  kilix panes dump 338 --lines 60\n"
            "  kilix panes wait 338 --for idle && "
            "kilix panes send 338 --enter 'continue'\n"
            "  printf 'status update' | kilix panes send 338 --enter"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", aliases=["ls"],
                                  help="print one pane snapshot")
    listing.add_argument("--json", action="store_true",
                         help="emit the stable kilix.panes/v1 document")
    listing.add_argument("-n", "--lines", type=_positive, default=0,
                         help="include the last N text lines for every pane")
    listing.add_argument("--screen", action="store_true",
                         help="with --lines, limit text to the visible screen")

    dumping = commands.add_parser("dump", help="print the last lines of one pane")
    dumping.add_argument("target", help="pane ID, title, or session-ID prefix")
    dumping.add_argument("-n", "--lines", type=_positive, default=40)
    dumping.add_argument("--screen", action="store_true",
                         help="limit the dump to the visible screen")
    dumping.add_argument("--json", action="store_true")

    sending = commands.add_parser(
        "send", aliases=["tell"], help="queue bounded text for exactly one pane")
    sending.add_argument("target", help="pane ID, title, or session-ID prefix")
    sending.add_argument("text", nargs="*", help="text; stdin is used when omitted")
    sending.add_argument("--enter", action="store_true",
                         help="append Enter, submitting a shell or agent prompt")
    sending.add_argument("--allow-self", action="store_true",
                         help="permit targeting the pane running this command")
    sending.add_argument("--json", action="store_true")

    focusing = commands.add_parser("focus", help="focus one resolved pane")
    focusing.add_argument("target")
    focusing.add_argument("--json", action="store_true")

    waiting = commands.add_parser(
        "wait", help="wait for a pane to reach an explicit activity state")
    waiting.add_argument("target")
    waiting.add_argument(
        "--for", dest="wanted", default="idle",
        choices=("idle", "working", "waiting", "agent", "shell", "remote", "running"),
    )
    waiting.add_argument("--timeout", type=_seconds, default=300.0)
    waiting.add_argument("--interval", type=_seconds, default=1.0)
    waiting.add_argument("--json", action="store_true")

    tui = commands.add_parser("tui", help="open the interactive pane center")
    tui.add_argument("--scope", choices=SCOPES, default="all")
    return parser


def _json(value: object) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _live(inspector: pane_center.Inspector | None = None) -> pane_center.Snapshot:
    if not kitty_rc.available():
        raise kitty_rc.Unavailable(
            "not running inside Kilix (remote-control context is unavailable)")
    return (inspector or pane_center.Inspector()).snapshot(kitty_rc.tree())


def _with_text(
    snapshot: pane_center.Snapshot,
    *,
    lines: int,
    screen: bool,
) -> dict[str, object]:
    payload = snapshot.to_dict()
    records = payload["panes"]
    assert isinstance(records, list)
    for item, record in zip(snapshot.panes, records):
        assert isinstance(record, dict)
        try:
            record["text"] = kitty_rc.pane_text(
                item.pane.id, lines=lines, scrollback=not screen)
        except kitty_rc.Unavailable as error:
            record["text"] = None
            record["text_error"] = str(error)
        record["text_line_limit"] = lines
    return payload


def _cmd_list(ns: argparse.Namespace) -> int:
    snapshot = _live()
    if ns.json:
        _json(_with_text(snapshot, lines=ns.lines, screen=ns.screen)
              if ns.lines else snapshot.to_dict())
        return 0
    columns = shutil.get_terminal_size(fallback=(120, 24)).columns
    print(pane_center.table(snapshot, width=columns))
    if ns.lines:
        for item in snapshot.panes:
            print(f"\n--- pane {item.pane.id}: {item.pane.title or item.pane.process} ---")
            try:
                print(kitty_rc.pane_text(
                    item.pane.id, lines=ns.lines, scrollback=not ns.screen))
            except kitty_rc.Unavailable as error:
                print(f"(unavailable: {error})")
    return 0


def _cmd_dump(ns: argparse.Namespace) -> int:
    snapshot = _live()
    item = snapshot.resolve(ns.target)
    text = kitty_rc.pane_text(
        item.pane.id, lines=ns.lines, scrollback=not ns.screen)
    if ns.json:
        _json({
            "schema": "kilix.panes.dump/v1",
            "pane": item.to_dict(),
            "text_line_limit": ns.lines,
            "text": text,
        })
    else:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _cmd_send(ns: argparse.Namespace) -> int:
    snapshot = _live()
    item = snapshot.resolve(ns.target)
    if item.pane.id == snapshot.self_pane_id and not ns.allow_self:
        raise ValueError(
            "refusing to type into this pane; choose another pane or use --allow-self")
    if ns.text:
        value = " ".join(ns.text)
    elif not sys.stdin.isatty():
        value = sys.stdin.read()
    elif ns.enter:
        value = ""
    else:
        raise ValueError("provide TEXT or pipe text on stdin")
    if ns.enter:
        if value.endswith("\r\n"):
            value = value[:-2] + "\r"
        elif value.endswith("\n"):
            value = value[:-1] + "\r"
        elif not value.endswith("\r"):
            value += "\r"
    if not value:
        raise ValueError("the message is empty")
    size = kitty_rc.send_text(item.pane, value)
    result = {
        "schema": "kilix.panes.send/v1",
        "accepted": True,
        "pane_id": item.pane.id,
        "broker_session": item.pane.broker_session,
        "bytes": size,
        "submitted": bool(ns.enter),
    }
    if ns.json:
        _json(result)
    else:
        submit = " and Enter" if ns.enter else ""
        print(f"pane {item.pane.id}: accepted {size} bytes{submit}")
    return 0


def _cmd_focus(ns: argparse.Namespace) -> int:
    item = _live().resolve(ns.target)
    kitty_rc.focus_pane(item.pane.id)
    if ns.json:
        _json({"focused": True, "pane_id": item.pane.id})
    else:
        print(item.pane.id)
    return 0


def _cmd_wait(ns: argparse.Namespace) -> int:
    inspector = pane_center.Inspector()
    started = time.monotonic()
    pane_id = 0
    last: pane_center.PaneInfo | None = None
    while True:
        snapshot = _live(inspector)
        if pane_id:
            last = snapshot.by_id(pane_id)
            if last is None:
                raise ValueError(f"pane {pane_id} closed while waiting")
        else:
            last = snapshot.resolve(ns.target)
            pane_id = last.pane.id
        elapsed = time.monotonic() - started
        if last.activity == ns.wanted:
            result = {
                "schema": "kilix.panes.wait/v1",
                "matched": True,
                "wanted": ns.wanted,
                "waited_seconds": elapsed,
                "pane": last.to_dict(),
            }
            if ns.json:
                _json(result)
            else:
                print(f"pane {pane_id}: {ns.wanted} after {elapsed:.1f}s")
            return 0
        if elapsed >= ns.timeout:
            if ns.json:
                _json({
                    "schema": "kilix.panes.wait/v1",
                    "matched": False,
                    "wanted": ns.wanted,
                    "waited_seconds": elapsed,
                    "pane": last.to_dict(),
                })
            else:
                print(
                    f"kilix panes: timed out waiting for pane {pane_id} "
                    f"to become {ns.wanted} (currently {last.activity})",
                    file=sys.stderr,
                )
            return 1
        time.sleep(max(0.1, ns.interval))


def cli(argv: list[str]) -> int:
    if argv and argv[0] in ("--json", "-j"):
        argv = ["list", "--json", *argv[1:]]
    elif argv and argv[0] in ("--lines", "-n"):
        argv = ["list", *argv]
    ns = _cli_parser().parse_args(argv)
    try:
        if ns.command in ("list", "ls"):
            return _cmd_list(ns)
        if ns.command == "dump":
            return _cmd_dump(ns)
        if ns.command in ("send", "tell"):
            return _cmd_send(ns)
        if ns.command == "focus":
            return _cmd_focus(ns)
        if ns.command == "wait":
            return _cmd_wait(ns)
    except (kitty_rc.Unavailable, ValueError) as error:
        print(f"kilix panes: {error}", file=sys.stderr)
        return 1
    return 0


def _tui(argv: list[str]) -> int:
    if argv and argv[0] == "tui":
        argv = argv[1:]
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
    # Typing filters panes, so '?' is text here.
    return app.run(
        render,
        state,
        handle=handle,
        tick_ms=2000,
        on_tick=tick,
        help_key=False,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cli_commands = {"list", "ls", "dump", "send", "tell", "focus", "wait"}
    if (argv and argv[0] in cli_commands) or (
        argv and argv[0] in ("--json", "-j", "--lines", "-n", "--help", "-h")
    ):
        return cli(argv)
    return _tui(argv)


if __name__ == "__main__":
    raise SystemExit(main())
