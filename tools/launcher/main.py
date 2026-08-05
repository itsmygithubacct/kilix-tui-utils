"""kilix-launcher — one catalog of everything this machine can launch.

Four desktops each owed the user the same three lists — the stack's own
programs, the applications freedesktop already describes, and the launchers
the user made — and none of them should parse `.desktop` files or grow a
second list UI to show them. This is that catalog once, as a text tool any
desktop opens in a tab (`kilix launcher`): stack programs from the desktop
registry, discovered XDG applications through the host SDK's shared scanner
(`kilix_sdk.xdgapps`, SDK 1.8), the user's desktop-folder launchers, the
executable scripts the stack checkouts ship, and a run-a-command row.

Launching keeps the registry's discipline: fixed argv into a Kilix page when
remote control is reachable, in place otherwise. Graphical applications go
through `kilix run`, which is the same containment the reference desktop
gives them; the run-a-command row splits its line shell-free, so nothing
here ever interprets shell syntax.
"""
from __future__ import annotations

import importlib
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_desk import registry, sources, tango  # noqa: E402
from kilix_tui import app, keys as keymap, kitty_rc, shell  # noqa: E402

SECTIONS = ("Stack", "Apps", "Launchers", "Scripts")
RUN_LABEL = "Run a command…"


# ── the shared scanner ───────────────────────────────────────────────────────

_XDG = None


def _sdk_xdgapps():
    """`kilix_sdk.xdgapps`, or None — resolved the way `theme.py` finds the
    SDK. None means an absent or pre-1.8 Kilix checkout; every caller
    degrades to a row that says so instead of failing."""
    global _XDG
    if _XDG is not None:
        return _XDG or None
    for home in (os.environ.get("KILIX_HOME", ""),
                 sources.component_dir("kilix")):
        config = os.path.join(home, "config") if home else ""
        if config and os.path.isdir(os.path.join(config, "kilix_sdk")):
            if config not in sys.path:
                sys.path.insert(0, config)
            try:
                _XDG = importlib.import_module("kilix_sdk.xdgapps")
                return _XDG
            except Exception:
                break
    _XDG = False
    return None


# ── rows ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Row:
    label: str
    argv: tuple[str, ...] | None = None   # None: disabled, `reason` says why
    right: str = ""                       # right-hand column text
    reason: str = ""
    command_row: bool = False             # the run-a-command prompt


def _kilix() -> list[str] | None:
    return registry.kilix_command()


def _wrap_gui(argv: list[str]) -> tuple[str, ...]:
    """Contain a graphical argv the way the reference desktop does: a
    `kilix run` tab. Without a Kilix checkout the bare argv is all there is."""
    kilix = _kilix()
    if kilix:
        return (*kilix, "run", *argv)
    return tuple(argv)


def stack_rows(live: bool = True) -> list[Row]:
    """The desktop registry's Programs, resolved; the catalog's first page."""
    rows = [Row(RUN_LABEL, right="type it, runs in a page", command_row=True)]
    for item in registry.SECTIONS["Programs"]:
        if item.submenu:
            continue                     # Games/Screensavers are desk places
        if item.kilix_only and not live:
            continue
        plan = registry.resolve(item)
        if plan is None:
            rows.append(Row(item.label, reason=registry.disabled_reason(item)))
            continue
        rows.append(Row(item.label, argv=tuple(plan.argv),
                        right=os.path.basename(plan.argv[-1])))
    return rows


def app_rows() -> list[Row]:
    """Discovered freedesktop applications, bucketed by the shared scanner."""
    sdk = _sdk_xdgapps()
    if sdk is None:
        return [Row("No application scanner",
                    reason="needs a Kilix checkout with SDK 1.8")]
    try:
        entries = sdk.scan()
    except Exception:
        entries = []
    rows = []
    for entry in entries:
        try:
            argv = shlex.split(entry.get("exec", ""))
        except ValueError:
            argv = []
        if not argv:
            continue
        if not entry.get("terminal"):
            argv = list(_wrap_gui(argv))
        rows.append(Row(entry.get("name", "app"), argv=tuple(argv),
                        right=sdk.bucket(entry)))
    if not rows:
        rows.append(Row("No applications found",
                        reason="nothing under $XDG_DATA_HOME / $XDG_DATA_DIRS"))
    return rows


def desktop_folder() -> str:
    """The desktop folder the reference desktop writes launchers into."""
    override = os.environ.get("KILIX_DESKTOP_DIR")
    if override:
        return override
    base = (os.environ.get("GPU_TERMINAL_HOME")
            or os.path.expanduser("~/.local/gpu_terminal"))
    return os.path.join(base, "kilix-95", "data", "desktop")


def launcher_rows(directory: str | None = None) -> list[Row]:
    """The user's own `.desktop` launchers, read with the shared parser."""
    sdk = _sdk_xdgapps()
    if sdk is None:
        return [Row("No launcher parser",
                    reason="needs a Kilix checkout with SDK 1.8")]
    directory = desktop_folder() if directory is None else directory
    rows = []
    try:
        names = sorted(n for n in os.listdir(directory)
                       if n.endswith(".desktop"))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(directory, name)
        parsed = sdk.parse_desktop_file(path)
        if not parsed:
            continue
        label = sdk.localized(parsed, "Name") or os.path.splitext(name)[0]
        try:
            argv = shlex.split(
                sdk.strip_field_codes(sdk.unescape(parsed.get("Exec", ""))))
        except ValueError:
            argv = []
        if not argv:
            continue
        # X-Kilix-Open is the launcher's own word for how it wants to open;
        # everything but a plain tab is a graphical intent → `kilix run`.
        mode = (parsed.get("X-Kilix-Open", "")
                or ("tab" if sdk.truthy(parsed.get("Terminal")) else "run"))
        if mode not in ("tab", "os-window", "fullscreen"):
            argv = list(_wrap_gui(argv))
        rows.append(Row(str(label), argv=tuple(argv), right="launcher"))
    if not rows:
        rows.append(Row("No launchers yet",
                        reason=f"nothing in {directory}"))
    return rows


def script_dirs() -> list[str]:
    """The stack's scripts/ directories, gated on presence like the
    reference desktop's System menu."""
    kilix_home = (os.environ.get("KILIX_HOME")
                  or sources.component_dir("kilix"))
    return [os.path.expanduser(os.path.join("~", "pleb", "scripts")),
            os.path.join(kilix_home, "scripts")]


def script_rows(dirs: list[str] | None = None) -> list[Row]:
    """Executable *.sh under the pleb/kilix scripts directories."""
    rows = []
    for base in (script_dirs() if dirs is None else dirs):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if name.endswith(".sh") and os.access(path, os.X_OK):
                short = base.replace(os.path.expanduser("~"), "~", 1)
                rows.append(Row(name, argv=(path,), right=short))
    if not rows:
        rows.append(Row("No scripts found",
                        reason="no executable *.sh under the stack checkouts"))
    return rows


# ── state ────────────────────────────────────────────────────────────────────


def _attached_run(argv) -> int:
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


@dataclass
class State:
    section: int = 0
    cursor: int = 0
    offset: int = 0
    message: str = ""
    mode: str = "browse"              # browse | command
    entry: str = ""                   # the command-row buffer
    filter: shell.Filter = field(default_factory=shell.Filter)
    rows_by_section: dict[int, list[Row]] = field(default_factory=dict)
    runner: object = _attached_run
    live: object = kitty_rc.available

    # Construction is deliberately pure — `main()` calls `refresh()` — so a
    # test can render a State without touching the machine it runs on.

    def refresh(self) -> None:
        self.rows_by_section = {
            0: stack_rows(live=bool(self.live())),
            1: app_rows(),
            2: launcher_rows(),
            3: script_rows(),
        }

    def rows(self) -> list[Row]:
        rows = self.rows_by_section.get(self.section, [])
        return self.filter.apply(rows, key=lambda row: row.label)

    def current(self) -> Row | None:
        rows = self.rows()
        if not rows:
            return None
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        return rows[self.cursor]


# ── drawing ──────────────────────────────────────────────────────────────────


def _put(surface, row: int, col: int, text: str, attr: int = 0) -> None:
    shell.put(surface, row, col, text, attr)


def _draw_rows(surface, state: State, top: int, left: int,
               height: int, width: int) -> None:
    if height <= 0 or width <= 0:
        return
    rows = state.rows()
    state.cursor = max(0, min(state.cursor, max(0, len(rows) - 1)))
    if state.cursor < state.offset:
        state.offset = state.cursor
    elif state.cursor >= state.offset + height:
        state.offset = state.cursor - height + 1
    state.offset = max(0, min(state.offset, max(0, len(rows) - height)))
    if not rows:
        note = (f"nothing matches “{state.filter.text}”"
                if state.filter.active() else "nothing here")
        _put(surface, top, left, note[:width], tango.attr("alert"))
        return
    for line in range(min(height, len(rows) - state.offset)):
        index = state.offset + line
        row = rows[index]
        selected = index == state.cursor
        marker = "▶" if selected else " "
        right = row.right or row.reason
        body = f"{marker} {row.label}"
        pad = width - len(body) - len(right) - 1
        if pad < 1:
            body = body[: max(0, width - len(right) - 2)]
            pad = 1
        text = f"{body}{' ' * pad}{right} "[:width]
        if selected:
            _put(surface, top + line, left, text.ljust(width),
                 tango.attr("selected"))
        elif row.argv is None and not row.command_row:
            _put(surface, top + line, left, text, tango.attr("muted"))
        else:
            _put(surface, top + line, left, text)


def footer(state: State) -> str:
    if state.mode == "command":
        return f"command: {state.entry}▏ · Enter run · Esc cancel"
    if state.filter.typing:
        return state.filter.footer()
    return ("Enter open · ! command · / filter · Tab section · r reload"
            " · q quit")


def render(surface, state: State) -> None:
    rows = state.rows()
    launchable = sum(1 for row in rows if row.argv or row.command_row)
    summary = state.message or state.filter.summary(launchable) or (
        f"{launchable} launchable"
        f"{' · ' + str(len(rows) - launchable) + ' unavailable' if len(rows) - launchable else ''}"
    )
    body = shell.draw(
        surface,
        title="Launcher",
        sections=SECTIONS,
        active=state.section,
        summary=summary,
        footer=footer(state),
        summary_role="accent" if state.message else "muted",
    )
    _draw_rows(surface, state, body.top, body.left, body.height, body.width)


# ── input ────────────────────────────────────────────────────────────────────


def _launch(state: State, argv: tuple[str, ...], title: str) -> None:
    if state.live():
        try:
            kitty_rc.launch_tab(list(argv), title=title)
            state.message = f"{title}: opened in a page"
            return
        except kitty_rc.Unavailable:
            pass                     # the floor: hand off in place instead
    code = state.runner(argv)
    state.message = f"{title} exited {code}" if code else ""


def _open(state: State) -> None:
    row = state.current()
    if row is None:
        return
    if row.command_row:
        state.mode, state.entry = "command", ""
        return
    if row.argv is None:
        state.message = row.reason
        return
    _launch(state, row.argv, row.label)


def _handle_command(key: int, state: State) -> bool:
    if key == keymap.ESCAPE:
        state.mode, state.entry = "browse", ""
    elif key in keymap.ENTER:
        line = state.entry.strip()
        state.mode, state.entry = "browse", ""
        if line:
            try:
                argv = shlex.split(line)
            except ValueError as error:
                state.message = f"not runnable: {error}"
                return True
            if argv:
                _launch(state, tuple(argv), argv[0])
    elif key in keymap.BACKSPACE:
        state.entry = state.entry[:-1]
    elif keymap.is_text(key):
        state.entry += chr(key)
    return True


def handle(key: int, state: State) -> bool:
    if state.mode == "command":
        return _handle_command(key, state)
    if state.filter.handle(key):
        state.cursor = 0
        return True
    if keymap.is_quit(key):
        return False
    if key in keymap.SELECT:
        _open(state)
        return True
    if (step := keymap.direction(key)):
        state.cursor = max(0, min(state.cursor + step,
                                  max(0, len(state.rows()) - 1)))
        return True
    if key == ord("!"):
        state.mode, state.entry = "command", ""
        return True
    if key == ord("\t"):
        state.section = (state.section + 1) % len(SECTIONS)
        state.cursor = state.offset = 0
        state.filter.clear()
        return True
    if ord("1") <= key <= ord(str(len(SECTIONS))):
        state.section = key - ord("1")
        state.cursor = state.offset = 0
        state.filter.clear()
        return True
    if keymap.is_refresh(key):
        state.refresh()
        state.message = ""
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
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle_:
            handle_.write(app.render_to_text(render, state) + "\n")
        return 0
    # `/` and `!` read typed text, so `?` stays an ordinary character here.
    return app.run(render, state, handle=handle, help_key=False)


if __name__ == "__main__":
    raise SystemExit(main())
