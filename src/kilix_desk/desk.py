"""The desktop itself: one list, one cursor, and a trail saying where you are.

Navigation is a *place*, not a focus model. There is one cursor and it is
always in the list you are looking at; Right or Enter walks into a place, Left
or Esc walks back out, and the breadcrumb on row one says which place that is.
The earlier design gave the section strip and the entry list their own focus,
so Up and Down meant two different things depending on invisible state — the
single fastest way to make a terminal UI feel unpredictable.

Everything else follows ncdu, which is the most navigable list in the terminal
and gets there with four habits worth copying: say where you are on every
screen, put "go back" *in* the list where the cursor can reach it, keep the
keys on screen instead of in a manual, and let `?` explain the rest in place.
To that we add `/`, because a system this size has more entries than a screen
has rows.

Three verbs and one rule. An entry is drawn here, handed the terminal in
place, or opened in a Kilix page — and in-place is the floor: every feature
must be reachable through it, because the page verb exists only inside Kilix.

This module is the text rendering — Tango-coloured curses, the floor every
terminal can carry. The pixel rendering over the same `State` lives in
`graphics.py`, and `main.py` picks per session. `section`, `submenu` and
`focus` remain readable and settable there and in the tests: they are views
over `path`, which is the one piece of navigation state.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

from kilix_tui import keys as keymap, kitty_rc, privileged, shell

from . import facts, registry, tango

SECTIONS = ("Home", "Programs", "Machine", "System", "Session", "Power")
QUIT_SENTINEL: tuple[str, ...] = ()
KEY_MOUSE = 409          # curses.KEY_MOUSE, named without importing curses
BACK_LABEL = ".."

# One line per place, shown above the footer. A tip earns its row by naming
# something the screen does not already show.
TIPS: dict[str, str] = {
    "": "Enter walks in, ← walks back — the trail above always says where you are",
    "Home": "r refreshes these numbers without leaving",
    "Programs": "press / and type to filter — 'chess' finds Chess Bash",
    "Machine": "these open live dashboards; q returns you here",
    "System": "settings, desktops and updates for the whole stack",
    "Session": "panes, pages and transcripts of what this terminal has shown",
    "Power": "every action here asks before it runs",
    "Software": "Enter installs; already-installed entries reinstall to their pin",
    "Catalog apps": "every application comes from the host's one content catalog",
    "Voice": "speech tools share the stack's models, settings and diagnostics",
    "Default desktop": "Enter chooses what every later session starts with",
    "Scripts": "the stack's own maintenance scripts; Enter runs one in place",
    "Games": "Enter toggles a game on or off for the whole stack",
    "Games+play": "Enter plays; t turns a game on or off for the whole stack",
    "Screensavers": "Enter runs one; any key stops it",
    "Applications": "what this machine itself installs — the stack list is above",
}


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
    back: bool = False               # the ".." row
    prompt: bool = False             # Enter opens the run-a-command prompt
    alt_argv: tuple[str, ...] | None = None   # `t` runs this quietly
    restart: bool = False            # a clean run re-execs the desktop


def entry_hint(entry: Entry) -> str:
    if entry.back:
        return "back"
    if entry.hint:
        return entry.hint
    if entry.submenu:
        return "▸"
    if entry.argv is None:
        return entry.reason
    if entry.verb == "tab":
        return "opens a page"
    if entry.verb == "report":
        return "shows output"
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


def _restart_desktop() -> None:
    """Replace this process with a fresh desktop.

    The point of "Update and restart desktop" is that the menu you come back
    to is drawn by the code the update just installed; a child process would
    leave the old desktop underneath it. curses is shut down first so the
    fresh process inherits a sane terminal. Reached again only when the exec
    itself failed — the caller words that failure.
    """
    try:
        import curses
        curses.endwin()
    except Exception:
        pass
    try:
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except OSError:
        pass


def report_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Wrap a command that prints and exits so its output can be read.

    `kilix status` is worth reaching from a menu, but running it directly
    would paint the screen and repaint over it in the same instant. The wrapper
    holds the output until Enter, which is the whole difference between a
    command that appears broken and one that answers a question.
    """
    quoted = " ".join(shlex.quote(part) for part in argv)
    return ("sh", "-c",
            f"{quoted}; printf '\\n— press Enter to return —'; read -r _")


def visible_window(count: int, height: int, selected: int) -> int:
    """The first visible index, keeping the selection on screen."""
    if height <= 0 or count <= height:
        return 0
    return max(0, min(selected - height + 1, count - height))


class State:
    def __init__(self, *, root_path: Sequence[str] = (),
                 application_name: str = "",
                 runner: Callable[[Sequence[str]], int] | None = None,
                 quiet: Callable[[Sequence[str]], int] | None = None,
                 live: Callable[[], bool] | None = None,
                 restart: Callable[[], None] | None = None) -> None:
        self.root_path = tuple(root_path)
        self.application_name = application_name
        self.path: list[str] = list(self.root_path)
        self.selected = 0
        self.filter = ""
        self.filtering = False
        self.help_open = False
        self.message = ""
        self.confirm: tuple[str, tuple[str, ...]] | None = None
        self.confirm_restart = False     # the pending confirm re-execs on success
        self.running_prompt = False      # the `!` run-a-command line is open
        self.command = ""                # what has been typed into it
        self.runner = runner or _attached_run
        self.quiet = quiet or _quiet_run
        self.live = live or kitty_rc.available
        self.restart = restart or _restart_desktop
        self.status = facts.status_rows()
        self.alerts = facts.alerts()
        # `entries()` runs on every keystroke, so anything that costs a
        # subprocess or a filesystem walk is fetched once per visit rather
        # than per frame. `r` drops all of it.
        self.software: list[dict] | None = None
        self.default_desktop: str | None = None
        self.apps: dict[str, list[dict]] | None = None
        self.scripts: list[dict] | None = None
        self.play_support: bool | None = None
        self.text_hits: dict = {}

    # ── views over `path`, kept for the pixel renderer and the tests ─────────

    @property
    def section(self) -> int:
        if not self.path:
            return 0
        try:
            return SECTIONS.index(self.path[0])
        except ValueError:
            return 0

    @section.setter
    def section(self, index: int) -> None:
        self.path = [SECTIONS[index % len(SECTIONS)]]
        self.selected = 0

    @property
    def submenu(self) -> str | None:
        return self.path[1].lower() if len(self.path) > 1 else None

    @submenu.setter
    def submenu(self, name: str | None) -> None:
        if name:
            if not self.path:
                self.path = ["Programs"]
            self.path = [self.path[0], name.capitalize()]
        elif len(self.path) > 1:
            self.path = self.path[:1]

    @property
    def focus(self) -> str:
        return "sections" if not self.path else "entries"

    @focus.setter
    def focus(self, value: str) -> None:
        # Entering "entries" from the root means walking into the section the
        # cursor is on; leaving them means walking back out.
        if value == "entries" and not self.path:
            self.path = [SECTIONS[min(self.selected, len(SECTIONS) - 1)]]
            self.selected = 0
        elif value == "sections" and self.path:
            self.path = []
            self.selected = 0

    # ── the data both renderers draw ─────────────────────────────────────────

    def place(self) -> str:
        """The name of the current place: "" at the root."""
        return self.path[-1] if self.path else ""

    def entries(self) -> list[Entry]:
        rows = self._place_entries()
        if self.filter:
            needle = self.filter.lower()
            rows = [row for row in rows if needle in row.label.lower()]
        if len(self.path) > len(self.root_path):
            rows.insert(0, Entry(BACK_LABEL, None, back=True))
        return rows

    def _place_entries(self) -> list[Entry]:
        if not self.path:
            return self._section_entries()
        if self.submenu == "default desktop":
            return self._default_desktop_entries()
        if self.submenu == "software":
            return self._software_entries()
        if self.submenu == "catalog apps":
            return self._catalog_application_entries()
        if self.submenu == "games":
            return self._game_entries()
        if self.submenu == "screensavers":
            return self._screensaver_entries()
        if self.submenu == "scripts":
            return self._script_entries()
        if self.submenu == "applications":
            return self._application_entries()
        if self.submenu == "voice":
            return self._registry_entries(registry.VOICE)
        name = self.path[0]
        if name == "Home":
            return []
        if name == "Power":
            return [Entry(label, tuple(argv), confirm=True)
                    for label, argv, _needs in privileged.power_actions()]
        live = bool(self.live())
        out: list[Entry] = []
        if name == "Programs":
            # F-RUN: the desk owns this row — it opens a prompt, not a
            # program, so it has no registry Item behind it.
            out.append(Entry("Run a command", None, prompt=True,
                             hint="type it yourself"))
        out.extend(self._registry_entries(registry.SECTIONS.get(name, ()), live=live))
        return out

    def _registry_entries(self, items, *, live: bool | None = None) -> list[Entry]:
        """Resolve one registry list through the desktop's common launch policy."""
        if live is None:
            live = bool(self.live())
        out: list[Entry] = []
        for item in items:
            if item.kilix_only and not live:
                continue
            if item.submenu:
                out.append(Entry(item.label, None, submenu=item.submenu))
                continue
            plan = registry.resolve(item)
            if plan is None:
                if item.helper:
                    # Presence-gated like the reference desktop's System
                    # menu: no OS helper, no row.
                    continue
                out.append(Entry(item.label, None,
                                 reason=registry.disabled_reason(item)))
                continue
            verb = plan.verb if (plan.verb == "inplace" or live) else "inplace"
            argv = (report_argv(plan.argv) if verb == "report"
                    else plan.argv)
            out.append(Entry(item.label, argv, verb=verb,
                             confirm=item.confirm, restart=item.restart))
        return out

    def _section_entries(self) -> list[Entry]:
        """The root: the six sections, as things the one cursor can open."""
        out: list[Entry] = []
        for name in SECTIONS:
            if name == "Home":
                hint = "status of the stack"
            elif name == "Power":
                hint = "shut down, reboot, log out"
            else:
                count = len(registry.SECTIONS.get(name, ()))
                hint = f"{count} entries" if count else ""
            out.append(Entry(name, None, submenu=name, hint=hint))
        return out

    DESKTOP_CHOICES = (
        ("auto", "pick the best available at login"),
        ("external", "Kilix 95"),
        ("xp", "Kilix 95, XP styling"),
        ("cap", "Kilix Cap, the mansion"),
        ("tui", "Kilix TUI, this desktop"),
        ("land", "Kilix Land, walkable"),
        ("builtin", "the bundled compatibility desktop"),
        ("none", "no desktop; a plain Kilix session"),
    )

    def _default_desktop_entries(self) -> list[Entry]:
        """Choose the desktop every later session starts with."""
        kilix = registry.kilix_command()
        if kilix is None:
            return [Entry("selecting a default needs a Kilix checkout", None,
                          reason="`kilix default-desktop` was not reachable")]
        if self.default_desktop is None:
            self.default_desktop = registry.default_desktop() or "auto"
        current = self.default_desktop
        return [
            Entry(f"{name}", (*kilix, "default-desktop", "set", name),
                  hint="current" if name == current else why)
            for name, why in self.DESKTOP_CHOICES
        ]

    def _software_entries(self) -> list[Entry]:
        """Everything installable, as `kilix install` reports it."""
        if self.software is None:
            self.software = registry.installable()
        rows = self.software
        kilix = registry.kilix_command()
        if rows is None or kilix is None:
            return [Entry("the installable list needs a Kilix checkout", None,
                          reason="`kilix install` was not reachable")]
        if not rows:
            return [Entry("nothing to install", None, reason="empty catalog")]
        out: list[Entry] = []
        for row in sorted(rows, key=lambda r: (r.get("kind", ""),
                                               str(r.get("label", "")).lower())):
            installed = bool(row.get("installed"))
            identifier = str(row.get("id", ""))
            label = str(row.get("label", identifier))
            # Installed entries stay listed and stay selectable: re-running an
            # install is how a pinned thing is brought back to its pin.
            out.append(Entry(
                f"{label}",
                (*kilix, "install", identifier),
                hint="installed" if installed else str(row.get("kind", "")),
            ))
        return out

    def _catalog_application_entries(self) -> list[Entry]:
        """Every catalog app, launched by the host in a page or in place."""
        if self.software is None:
            self.software = registry.installable()
        rows = self.software
        kilix = registry.kilix_command()
        if rows is None or kilix is None:
            return [Entry("the application catalog needs a Kilix checkout", None,
                          reason="`kilix install --json` was not reachable")]
        apps = [row for row in rows if row.get("kind") == "app"]
        if not apps:
            return [Entry("no catalog applications", None,
                          reason="the host catalog returned no apps")]
        verb = "tab" if self.live() else "inplace"
        return [
            Entry(
                str(row.get("label") or row.get("id") or "Application"),
                (*kilix, "app", "run", str(row.get("id", ""))),
                verb=verb,
                hint=("installed" if row.get("installed")
                      else "installs on first use"),
            )
            for row in sorted(
                apps, key=lambda item: str(item.get("label", "")).casefold())
            if row.get("id")
        ]

    def _game_entries(self) -> list[Entry]:
        rows = registry.games()
        kilix = registry.kilix_command()
        if rows is None or kilix is None:
            return [Entry("games need a Kilix checkout", None,
                          reason="no kilix_sdk reachable")]
        if self.play_support is None:
            self.play_support = registry.games_play_supported(kilix)
        out = []
        for game_id, label, enabled in rows:
            action = "disable" if enabled else "enable"
            flip = (*kilix, "games", action, game_id)
            if self.play_support:
                # Enter plays — the natural reading of a games list — and `t`
                # keeps the availability toggle one key away.
                out.append(Entry(label, (*kilix, "games", "play", game_id),
                                 verb="tab", hint="on" if enabled else "off",
                                 alt_argv=flip))
            else:
                # Older launchers know no `play`: Enter stays the toggle so
                # the list is never a dead end.
                out.append(Entry(label, flip, toggle=True,
                                 hint="on" if enabled else "off"))
        return out

    def _application_entries(self) -> list[Entry]:
        """Discovered freedesktop apps: buckets at depth two, apps below.

        Terminal applications launch like any tool (a page inside Kilix, in
        place elsewhere). GUI applications go through `kilix run`, the same
        containment Kilix 95 uses for browsers — so without a Kilix checkout
        they are listed disabled with the reason, not launched raw onto
        whatever display may or may not exist.
        """
        if self.apps is None:
            self.apps = registry.applications()
        groups = self.apps
        if not groups:
            return [Entry("no applications discovered", None,
                          reason="no .desktop entries under the XDG dirs")]
        if len(self.path) == 2:
            return [Entry(bucket, None, submenu=bucket,
                          hint=f"{len(rows)} apps")
                    for bucket, rows in groups.items()]
        bucket = self.path[2]
        kilix = registry.kilix_command()
        out: list[Entry] = []
        for app in groups.get(bucket, ()):
            try:
                argv = tuple(shlex.split(app["exec"]))
            except ValueError:
                continue
            if not argv:
                continue
            if app.get("terminal"):
                out.append(Entry(app["name"], argv, verb="tab"))
            elif kilix is None:
                out.append(Entry(app["name"], None,
                                 reason="needs a Kilix checkout to contain it"))
            else:
                out.append(Entry(app["name"], (*kilix, "run", *argv),
                                 verb="tab"))
        if not out:
            return [Entry("nothing launchable in this bucket", None,
                          reason="every entry here failed to parse")]
        return out

    def _script_entries(self) -> list[Entry]:
        """The stack's executable maintenance scripts, run in place.

        The listing is `registry.script_rows()`, shared with the launcher
        catalog, so the two surfaces can never disagree about what a script
        is: executable `*.sh` under the stack's own scripts directories.
        """
        if self.scripts is None:
            self.scripts = registry.script_rows()
        if not self.scripts:
            return [Entry("no scripts installed", None,
                          reason="no executable *.sh under the stack's "
                                 "scripts directories")]
        return [Entry(str(row["label"]), tuple(row["argv"]))
                for row in self.scripts]

    def _screensaver_entries(self) -> list[Entry]:
        kilix = registry.kilix_command()
        names = registry.screensavers()
        if kilix is None or not names:
            return [Entry("screensavers need a Kilix checkout", None,
                          reason="no Kilix checkout reachable")]
        return [Entry(name, (*kilix, "screensaver", name)) for name in names]

    def breadcrumb(self) -> str:
        if self.application_name:
            tail = self.path[len(self.root_path):]
            return " › ".join([self.application_name, *tail])
        return " › ".join(["Kilix", *self.path])

    def tip(self) -> str:
        if self.running_prompt:
            return "runs in a page inside Kilix, in place anywhere else"
        if self.filtering:
            return "type to narrow the list · Esc clears it · Enter keeps it"
        if self.place() == "Games" and self.play_support:
            return TIPS["Games+play"]
        return TIPS.get(self.place(), TIPS[""])


# ── drawing ──────────────────────────────────────────────────────────────────


def _put(surface, row: int, col: int, text: str, attr: int = 0) -> None:
    try:
        surface.addstr(row, col, text, attr)
    except Exception:
        pass


def _draw_home(surface, state: State, top: int,
               height: int, width: int) -> None:
    row = top
    for line in state.alerts:
        if row >= top + height - 1:
            break
        _put(surface, row, 2, f"! {line}"[: width - 3], tango.attr("alert"))
        row += 1
    for label, value in state.status:
        if row >= top + height - 1:
            break
        _put(surface, row, 2, f"{label:<18}"[: width - 3], tango.attr("muted"))
        _put(surface, row, min(20, width - 1), str(value)[: width - 21],
             tango.attr("title"))
        row += 1


def _hint_attr(entry: Entry) -> int:
    if entry.back:
        return tango.attr("muted")
    if entry.hint:
        return tango.attr("accent" if entry.hint == "on" else "muted")
    if entry.submenu:
        return tango.attr("accent")
    if entry.argv is None:
        return tango.attr("muted")
    if entry.verb in ("tab", "report"):
        return tango.attr("accent")
    if entry.confirm:
        return tango.attr("alert")
    return 0


def _draw_entries(surface, state: State, top: int,
                  height: int, width: int) -> None:
    entries = state.entries()
    if not entries:
        empty = ("nothing matches that filter" if state.filter
                 else "nothing here")
        _put(surface, top, 2, empty[: width - 3], tango.attr("alert"))
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
        marker = "▶" if selected else " "
        body = f" {marker} {entry.label}"
        pad = width - len(body) - len(hint) - 2
        if pad < 1:
            body = body[: max(0, width - len(hint) - 3)]
            pad = 1
        if selected:
            attr = tango.attr("danger" if entry.confirm else "selected")
            _put(surface, top + line, 0,
                 f"{body}{' ' * pad}{hint} ".ljust(width - 1)[: width - 1],
                 attr)
        else:
            body_attr = 0
            if entry.back:
                body_attr = tango.attr("muted")
            elif entry.argv is None and not entry.submenu:
                body_attr = tango.attr("muted")
            _put(surface, top + line, 0, body[: width - 1], body_attr)
            if hint:
                _put(surface, top + line, max(0, width - len(hint) - 2),
                     hint, _hint_attr(entry))


def footer(state: State, width: int = 0) -> str:
    if state.confirm is not None:
        return "y confirm · any other key cancels"
    if state.running_prompt:
        return "type a command · Enter runs it · Esc cancels"
    if state.filtering:
        return "type to filter · Enter keep · Esc clear · ↑↓ move"
    return keymap.footer(width)


def render(surface, state: State) -> None:
    height, width = surface.getmaxyx()
    state.text_hits = {"bar_row": 1, "sections": []}
    release = next((v for k, v in state.status if k == "release"), "")
    strap = f"Plebian-OS {release}".rstrip()
    if state.confirm is not None:
        summary = f"Confirm: {state.confirm[0]}"
        summary_role = "danger"
    elif state.message:
        summary = state.message
        summary_role = "accent"
    elif state.filter:
        has_back = len(state.path) > len(state.root_path)
        count = max(0, len(state.entries()) - int(has_back))
        summary = f"filter: {state.filter}_  ({count} match"
        summary += ")" if count == 1 else "es)"
        summary_role = "accent"
    else:
        summary = _place_summary(state)
        summary_role = "muted"
    body = shell.draw(
        surface,
        title=strap,
        summary=summary,
        footer=footer(state, max(0, width - 2)),
        summary_role=summary_role,
        breadcrumb=state.breadcrumb(),
        tip=state.tip(),
    )

    if state.confirm is not None:
        label, argv = state.confirm
        _put(surface, body.top, body.left + 1,
             f"Confirm: {label}"[: body.width - 1], tango.attr("danger"))
        if argv:
            _put(surface, body.top + 1, body.left + 1,
                 f"$ {' '.join(argv)}"[: body.width - 1], tango.attr("muted"))
    elif state.place() == "Home":
        # Home is a place like any other, so the way out of it has to be on
        # screen. Drawing only the status rows left the cursor sitting on a
        # ".." the user could not see: Enter went back, and nothing said so.
        _draw_entries(surface, state, body.top, 1, width)
        _draw_home(surface, state, body.top + 2, max(0, body.height - 2), width)
    else:
        _draw_entries(surface, state, body.top, body.height, width)

    if state.help_open:
        shell.overlay(surface, title="Kilix TUI keys",
                      rows=list(keymap.help_rows()),
                      note="any key closes this")


def _place_summary(state: State) -> str:
    if not state.path:
        return "Everything Kilix can do, in six places"
    entries = [entry for entry in state.entries() if not entry.back]
    missing = sum(1 for entry in entries
                  if entry.argv is None and not entry.submenu)
    if not entries:
        return state.place()
    if missing:
        return f"{len(entries)} entries · {missing} not installed"
    return f"{len(entries)} entries"


# ── input ────────────────────────────────────────────────────────────────────


def _open_prompt(state: State) -> None:
    state.running_prompt = True
    state.command = ""
    state.message = "$ ▌"


def _run_command(state: State) -> None:
    """Execute what the prompt holds: a page inside Kilix, in place outside.

    The command is split like a shell would split it but never given to one —
    the same argv-only discipline every launcher in this fleet follows. What
    it cannot do is pipes and redirects, and the message says so rather than
    letting `foo | bar` fail somewhere less explainable.
    """
    state.running_prompt = False
    text = state.command.strip()
    state.command = ""
    if not text:
        state.message = ""
        return
    try:
        argv = tuple(shlex.split(text))
    except ValueError as error:
        state.message = f"unparsable: {error}"
        return
    if any(part in ("|", ">", "<", ">>", "&&", "||", ";") for part in argv):
        state.message = "plain commands only — pipes need a terminal"
        return
    if not argv:
        state.message = ""
        return
    _launch(state, Entry(text, argv, verb="tab"))


def _run_key(key: int, state: State) -> bool:
    """Keys while the `!` prompt is open: text builds the command."""
    if key == keymap.ESCAPE:
        state.running_prompt = False
        state.command = ""
        state.message = "cancelled"
        return True
    if key in keymap.ENTER:
        _run_command(state)
        return True
    if key in keymap.BACKSPACE:
        state.command = state.command[:-1]
        state.message = f"$ {state.command}▌"
        return True
    if keymap.is_text(key):
        state.command += chr(key)
        state.message = f"$ {state.command}▌"
        return True
    return True


def _resolve_program(name: str) -> str | None:
    """An absolute path for `name`, resolved in the desk's own environment.

    PATH first, then ~/.local/bin explicitly: that is where the stack
    installs its tools, and desktop sessions do not reliably carry it on
    PATH. Names that are already paths only have to be executable.
    """
    if os.path.sep in name:
        return name if os.access(name, os.X_OK) else None
    if found := shutil.which(name):
        return found
    local = os.path.join(os.path.expanduser("~"), ".local", "bin", name)
    return local if os.access(local, os.X_OK) else None


def _launch(state: State, entry: Entry) -> None:
    """The verbs shared by list entries and the run prompt."""
    # Resolved here, not by the terminal: kitty spawns a page's child from
    # its own environment, whose PATH may lack ~/.local/bin — the child then
    # dies before its first prompt and the page is a corpse whose only trace
    # is a resize warning (the 0.1.7 review's dead rollout-resume tab). An
    # absolute argv[0] launches the same everywhere, and a name that cannot
    # be resolved fails right here, with words.
    argv = list(entry.argv)
    program = _resolve_program(argv[0])
    if program is None:
        state.message = (f"{entry.label}: {argv[0]} is not installed "
                         "(not on PATH or in ~/.local/bin)")
        return
    argv[0] = program
    if entry.verb == "tab" and state.live():
        try:
            kitty_rc.launch_tab(argv, title=entry.label)
            state.message = f"{entry.label}: opened in a page"
            return
        except kitty_rc.Unavailable:
            pass                     # the floor: hand off in place instead
    code = state.runner(tuple(argv))
    state.message = f"{entry.label} exited {code}" if code else ""


def _open(state: State, entry: Entry) -> None:
    if entry.back:
        _back(state)
        return
    if entry.prompt:
        _open_prompt(state)
        return
    if entry.submenu:
        # A section from the root, or a drill-down inside one.
        name = entry.submenu
        state.path = ([name] if name in SECTIONS
                      else [*state.path, name.capitalize()])
        state.selected = 0
        state.message = ""
        state.filter = ""
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
        state.confirm_restart = entry.restart
        return
    _launch(state, entry)


def _back(state: State) -> None:
    if state.filter:
        state.filter = ""
        state.filtering = False
        state.selected = 0
        return
    if len(state.path) > len(state.root_path):
        leaving = state.path[-1]
        state.path = state.path[:-1]
        state.message = ""
        # Land the cursor on the place just left, so walking out and back in
        # returns to where you were rather than to the top of the list.
        siblings = [entry.label for entry in state.entries()]
        state.selected = (siblings.index(leaving)
                          if leaving in siblings else 0)


def _enter_section(state: State, section: int) -> None:
    if state.root_path:
        return
    state.path = [SECTIONS[section % len(SECTIONS)]]
    state.selected = 0
    state.message = ""
    state.filter = ""
    state.filtering = False


def _quit(state: State) -> bool:
    # When the desktop is the whole session, quitting means a respawn or a
    # black screen, so it asks first. In a pane or over ssh it just quits.
    if not state.root_path and os.environ.get("KILIX_TUI_SESSION") == "1":
        state.confirm = ("Leave this session", QUIT_SENTINEL)
        return True
    return False


def _select(state: State) -> None:
    entries = state.entries()
    if entries and state.selected < len(entries):
        _open(state, entries[state.selected])


def _move(state: State, step: int) -> None:
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
        _move(state, -1)
        return True
    if bstate & wheel_down:
        _move(state, 1)
        return True
    if not bstate & clicked:
        return True
    top = hits.get("top")
    if top is None or not (top <= y < top + hits.get("visible", 0)):
        return True
    index = hits.get("offset", 0) + (y - top)
    if index == state.selected:
        entries = state.entries()
        if index < len(entries):
            _open(state, entries[index])
    else:
        state.selected = index
    return True


def getattr_int(name: str) -> int:
    try:
        import curses
        return int(getattr(curses, name))
    except Exception:
        return 0


def _filter_key(key: int, state: State) -> bool:
    """Keys while `/` is open: text builds the needle, arrows still move."""
    if key == keymap.ESCAPE:
        state.filter = ""
        state.filtering = False
        state.selected = 0
        return True
    if key in keymap.ENTER:
        state.filtering = False          # keep the needle, resume navigating
        return True
    if key in keymap.BACKSPACE:
        state.filter = state.filter[:-1]
        state.selected = 0
        return True
    if (step := keymap.direction(key)) and not keymap.is_text(key):
        _move(state, step)
        return True
    if keymap.is_text(key):
        state.filter += chr(key)
        state.selected = 0
        return True
    return True


def handle(key: int, state: State) -> bool:
    if state.help_open:
        state.help_open = False          # any key closes it, as the note says
        return True
    if state.confirm is not None:
        label, argv = state.confirm
        restart = state.confirm_restart
        state.confirm = None
        state.confirm_restart = False
        if key in (ord("y"), ord("Y")):
            if argv == QUIT_SENTINEL:
                return False
            code = state.runner(argv)
            if code == 0 and restart:
                # Replaced on success; still here means the exec failed
                # (or a test stubbed it), so say what remains to be done.
                state.restart()
                state.message = f"{label}: restart the desktop to finish"
                return True
            state.message = (f"{label} exited {code}" if code
                             else f"{label}: done")
        else:
            state.message = f"cancelled: {label}"
        return True
    if key == KEY_MOUSE:
        return _text_mouse(state)
    if state.running_prompt:
        return _run_key(key, state)
    if state.filtering:
        return _filter_key(key, state)
    if keymap.is_help(key):
        state.help_open = True
        return True
    if keymap.is_filter(key):
        state.filtering = True
        state.message = ""
        return True
    if key == ord("!"):
        _open_prompt(state)
        return True
    if key in (ord("t"), ord("T")):
        entries = state.entries()
        if state.selected < len(entries):
            entry = entries[state.selected]
            if entry.alt_argv is not None:
                code = state.quiet(entry.alt_argv)
                state.message = (f"{entry.label}: {entry.alt_argv[-2]}d"
                                 if not code
                                 else f"{entry.label}: failed ({code})")
                return True
    if key == keymap.ESCAPE:
        # Esc walks out one level at a time, then asks before leaving.
        if state.filter or len(state.path) > len(state.root_path):
            _back(state)
            return True
        return _quit(state)
    if key in (ord("q"), ord("Q")):
        return _quit(state)
    if ord("1") <= key <= ord("0") + len(SECTIONS):
        _enter_section(state, key - ord("1"))
        return True
    if key == ord("\t"):
        if not state.root_path:
            _enter_section(state, self_next_section(state))
        return True
    if key in keymap.LEFT:
        _back(state)
        return True
    if key in keymap.RIGHT:
        _select(state)
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
        state.selected = 0
        return True
    if key in keymap.END:
        state.selected = max(0, len(state.entries()) - 1)
        return True
    if key in keymap.SELECT:
        _select(state)
        return True
    if keymap.is_refresh(key):
        state.status = facts.status_rows()
        state.alerts = facts.alerts()    # re-ask the security helper
        state.software = None            # re-ask the launcher for the list
        state.default_desktop = None
        state.apps = None                # rescan the .desktop entries
        state.scripts = None             # relist the maintenance scripts
        state.play_support = None        # re-probe the launcher's verbs
        state.message = ""
        return True
    return True


def self_next_section(state: State) -> int:
    """Tab cycles sections from wherever you are."""
    return (state.section + 1) % len(SECTIONS)
