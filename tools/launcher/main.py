"""kilix-launcher — the one catalog, for desktops that delegate to a tab.

Kilix 95 grows menus, the TUI desktop grows places; the mansion and the house
should grow neither — their idiom is an object that opens a surface in a tab.
This is that surface: stack programs, the machine's discovered freedesktop
applications, the user's own desktop-folder launchers, the stack's scripts,
and the laptop session profiles, in one filterable list with a run-a-command
row. `kilix launcher` opens it; picking a launch row replaces this process
with the launch, so the tab becomes the thing chosen; laptop rows act in
place and the list refreshes.

The list is assembled from the same single sources every other surface reads
— `kilix_desk.registry` for the stack, `kilix_tui.xdgapps` for discovery,
`kilix laptop status` for the shared run registry — so this can never
disagree with the TUI desktop about what exists, nor with the desktops'
laptops about what is running.

Launch rules match the desktop's: an argv is executed, never a shell line;
graphical applications are contained in a `kilix run` page; `report`-verb
programs hold their output until Enter. `--list` prints the catalog and
exits, which is what a test (or another desktop's tool) wants.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_desk import registry                              # noqa: E402
from kilix_desk.desk import report_argv                      # noqa: E402
from kilix_desk.registry import script_dirs, script_rows     # noqa: E402
from kilix_tui import app, keys as keymap, shell, xdgapps    # noqa: E402

RUN_ROW = "Run a command…"


def launcher_dirs() -> list[str]:
    """The user's desktop-launcher folder: override first, then the roots
    Kilix 95 and the host's bundled desktop actually write."""
    override = os.environ.get("KILIX_DESKTOP_DIR")
    if override:
        return [override]
    base = os.environ.get("GPU_TERMINAL_HOME") or os.path.expanduser(
        "~/.local/gpu_terminal")
    return [os.path.join(base, "kilix-95", "data", "desktop"),
            os.path.join(base, "kilix", "data", "desktop")]


# The scripts listing lives in `kilix_desk.registry` (shared with the desk's
# System ▸ Scripts place); `script_dirs`/`script_rows` are imported above.

# ── the laptop rows ─────────────────────────────────────────────────────────

_LAPTOP_VERB = None


def _laptop_verb_available(kilix: list[str] | None) -> bool:
    """Whether the host's kilix knows `kilix laptop`, probed once the way
    the games handoff probes `kilix games play`: run `kilix laptop help`,
    bounded, and require the usage token — an old launcher forwards
    unknown words to the terminal engine, so exit codes alone lie."""
    global _LAPTOP_VERB
    if _LAPTOP_VERB is not None:
        return _LAPTOP_VERB
    _LAPTOP_VERB = False
    if kilix:
        try:
            probe = subprocess.run(
                [*kilix, "laptop", "help"], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if probe.returncode == 0 and "open PROFILE" in probe.stdout:
            _LAPTOP_VERB = True
    return _LAPTOP_VERB


def laptop_rows(kilix: list[str] | None) -> list[dict]:
    """The laptop session profiles, straight from `kilix laptop status` —
    the host verb owns the shared run registry, so what this list calls
    running is exactly what every desktop's laptop shows open. A running
    profile's row closes its session; a stopped one opens it. Both act in
    place (the verb returns once the work is handed off) and the catalog
    refreshes to show the registry's new word."""
    def note(label: str, reason: str) -> dict:
        return {"kind": "note", "label": label, "detail": reason}

    if not kilix or not _laptop_verb_available(kilix):
        return [note("No laptop profiles",
                     "needs a Kilix with the `kilix laptop` verb")]
    try:
        report = subprocess.run(
            [*kilix, "laptop", "status"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return [note("No laptop profiles",
                     "`kilix laptop status` did not answer")]
    if report.returncode != 0:
        return [note("No laptop profiles",
                     "`kilix laptop status` did not answer")]
    out: list[dict] = []
    for line in report.stdout.splitlines():
        profile_id, _, state = line.partition(" ")
        if not profile_id:
            continue
        if state.startswith("running"):
            out.append({"kind": "laptop", "label": profile_id,
                        "detail": f"{state} · Enter closes",
                        "action": [*kilix, "laptop", "close", profile_id]})
        elif state in ("stopped", "desktop"):
            out.append({"kind": "laptop", "label": profile_id,
                        "detail": state,
                        "action": [*kilix, "laptop", "open", profile_id]})
        else:
            out.append(note(profile_id, "profile does not parse"))
    if not out:
        out.append(note("No profiles yet",
                        "~/.local/gpu_terminal/laptop is empty"))
    return out


def rows() -> list[dict]:
    """The catalog: kind, label, detail, and how to launch each row."""
    out: list[dict] = [{"kind": "run", "label": RUN_ROW,
                        "detail": "type an argv; ! from anywhere"}]
    kilix = registry.kilix_command()
    for item in registry.PROGRAMS:
        if item.submenu:
            continue
        plan = registry.resolve(item)
        if plan is None:
            continue
        out.append({"kind": "program", "label": item.label,
                    "detail": "stack", "argv": list(plan.argv),
                    "verb": plan.verb})
    for bucket, entries in xdgapps.grouped().items():
        for entry in entries:
            row = _app_row(entry, bucket.lower(), kilix)
            if row is not None:
                out.append(row)
    seen_launchers = set()
    for directory in launcher_dirs():
        for entry in xdgapps.entries_in(directory):
            if entry["id"] in seen_launchers:
                continue
            seen_launchers.add(entry["id"])
            row = _app_row(entry, "launcher", kilix)
            if row is not None:
                out.append(row)
    out.extend(script_rows())
    out.extend(laptop_rows(kilix))
    return out


def _app_row(entry: dict, detail: str, kilix: list[str] | None) -> dict | None:
    try:
        argv = shlex.split(entry["exec"])
    except ValueError:
        return None
    if not argv:
        return None
    if entry.get("terminal"):
        return {"kind": "app", "label": entry["name"], "detail": detail,
                "argv": argv, "verb": "inplace"}
    if kilix is None:
        return None            # nothing safe to contain a GUI app with
    return {"kind": "app", "label": entry["name"], "detail": detail,
            "argv": [*kilix, "run", *argv], "verb": "inplace"}


def launch_argv(row: dict) -> list[str] | None:
    """What execing this row means; None for the run row."""
    argv = row.get("argv")
    if not argv:
        return None
    if row.get("verb") == "report":
        return list(report_argv(argv))
    return list(argv)


class State:
    def __init__(self) -> None:
        self.all = rows()
        self.filter = ""
        self.selected = 0
        self.command: str | None = None      # non-None: the run line is open
        self.message = ""

    @property
    def rows(self) -> list[dict]:
        needle = self.filter.lower()
        return [r for r in self.all if needle in str(r["label"]).lower()]


def _exec(argv: list[str], state: State) -> bool:
    """Replace this process with the launch; stay alive only on failure."""
    try:
        os.execvp(argv[0], argv)
    except OSError as error:
        state.message = f"{argv[0]}: {error.strerror or error}"
    return True


def _act(row: dict, state: State) -> bool:
    """Run an in-place action (laptop open/close), then refresh the
    catalog so the row states match the registry again."""
    try:
        done = subprocess.run(
            row["action"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        state.message = f"{row['label']}: {error}"
        return True
    if done.returncode == 0:
        state.message = f"{row['label']}: done"
    else:
        noise = (done.stderr or done.stdout or "").strip()
        state.message = noise.splitlines()[-1] if noise else (
            f"{row['label']}: exit {done.returncode}")
    state.all = rows()
    state.selected = min(state.selected, max(0, len(state.rows) - 1))
    return True


def activate(state: State) -> bool:
    rows_now = state.rows
    if not rows_now:
        return True
    row = rows_now[min(state.selected, len(rows_now) - 1)]
    if row["kind"] == "run":
        state.command = ""
        return True
    if row.get("action"):
        return _act(row, state)
    argv = launch_argv(row)
    if argv is None:
        return True
    return _exec(argv, state)


def run_command(state: State) -> bool:
    text = (state.command or "").strip()
    state.command = None
    if not text:
        return True
    try:
        argv = shlex.split(text)
    except ValueError as error:
        state.message = f"unparsable: {error}"
        return True
    if any(part in ("|", ">", "<", ">>", "&&", "||", ";") for part in argv):
        state.message = "plain commands only — pipes need a terminal"
        return True
    if not argv:
        return True
    return _exec(argv, state)


def render(surface, state: State) -> None:
    rows_now = state.rows
    if state.command is not None:
        summary = f"$ {state.command}▌"
    elif state.message:
        summary = state.message
    else:
        programs = sum(1 for r in state.all if r["kind"] == "program")
        apps = sum(1 for r in state.all if r["kind"] == "app")
        summary = (f"{programs} stack programs · {apps} applications"
                   f"{f' · filter /{state.filter}' if state.filter else ''}")
    footer = ("type a command · Enter runs it · Esc cancels"
              if state.command is not None
              else "type to filter · Enter launch · ! run · Esc quit")
    body = shell.draw(
        surface,
        help_key=False,      # typing filters, so '?' stays text here
        title="Launcher",
        sections=("Everything",),
        summary=summary,
        footer=footer,
    )
    if not rows_now:
        shell.put(surface, body.top, body.left,
                  "nothing matches that filter", shell.tango.attr("alert"))
        return
    visible = max(1, body.height)
    start = max(0, min(state.selected - visible // 2,
                       max(0, len(rows_now) - visible)))
    for index, row in enumerate(rows_now[start:start + visible]):
        y = body.top + index
        selected = start + index == state.selected
        marker = "▶" if selected else " "
        shell.put(
            surface, y, body.left,
            f"{marker} {str(row['label']):<44.44} {row['detail']}",
            shell.tango.attr("selected") if selected else 0,
        )


def handle(key: int, state: State) -> bool:
    if state.command is not None:
        if key == keymap.ESCAPE:
            state.command = None
            return True
        if key in keymap.ENTER:
            return run_command(state)
        if key in keymap.BACKSPACE:
            state.command = state.command[:-1]
            return True
        if keymap.is_text(key):
            state.command += chr(key)
            return True
        return True
    if key == keymap.ESCAPE or key in (ord("q"), ord("Q")):
        return False
    if key == ord("!"):
        state.command = ""
        state.message = ""
        return True
    if key in keymap.ENTER:
        return activate(state)
    if key in keymap.BACKSPACE:
        state.filter = state.filter[:-1]
        return True
    if (step := keymap.direction(key)) and state.rows:
        state.selected = max(0, min(len(state.rows) - 1,
                                    state.selected + step))
        return True
    if keymap.is_text(key):
        state.filter += chr(key)
        state.selected = 0
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--list" in argv:
        for row in rows():
            command = " ".join(row.get("argv", row.get("action", []))) or "-"
            print(f"{row['kind']}\t{row['label']}\t{command}")
        return 0
    state = State()
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle_:
            handle_.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle, help_key=False)


if __name__ == "__main__":
    raise SystemExit(main())
