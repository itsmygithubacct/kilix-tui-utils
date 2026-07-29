"""plebian-os — the control TUI for the operating system.

Every action here shells out to a command that already exists: `pleb`,
`plebian-os-update`, `systemctl`. Nothing is reimplemented, because those
commands already own the update transaction, the lock, and the rollback, and a
second implementation of any of that would be a second thing to get wrong.

Power lives here for a specific reason: the only shutdown path in the whole
stack is inside Kilix 95, so a session running with no desktop provider has no
way to turn the machine off.
"""
from __future__ import annotations

import shutil
import subprocess

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, privileged, proc  # noqa: E402

SECTIONS = ("Status", "Update", "Session", "Power", "Health")


def _run(args: list[str], timeout: int = 20) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=timeout, check=False)
        return (result.stdout or result.stderr).strip()
    except FileNotFoundError:
        return f"({args[0]} not installed)"
    except (OSError, subprocess.SubprocessError) as error:
        return f"({error})"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def build_info() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read("/etc/plebian-os/build-info.env").splitlines():
        key, _, value = line.strip().partition("=")
        if key and not key.startswith("#"):
            values[key] = value.strip("'\"")
    return values


def component_versions() -> list[tuple[str, str]]:
    source = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or os.path.join(
        os.path.expanduser("~"), "gpu_terminal")
    rows = []
    for name in ("plebian-os", "pleb", "kilix", "kilix-95", "kilix-tui-utils"):
        version = _read(os.path.join(source, name, "VERSION")).strip()
        rows.append((name, version or "not present"))
    return rows


def status_rows() -> list[tuple[str, str]]:
    info = build_info()
    rows: list[tuple[str, str]] = []
    if release := info.get("PLEBIAN_OS_VERSION"):
        rows.append(("release", release))
    rows.extend(component_versions())
    storage = os.environ.get("KILIX_STORAGE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local/gpu_terminal/kilix")
    generation = os.path.realpath(os.path.join(storage, "build/current"))
    if os.path.isdir(generation):
        rows.append(("engine", os.path.basename(generation)))
        source_id = _read(os.path.join(generation, "source-id")).strip()
        if source_id:
            rows.append(("source-id", source_id[:12]))
    rows.append(("provider", os.environ.get("KILIX_DESKTOP_PROVIDER", "auto")))
    rows.append(("uptime", proc.human_duration(proc.uptime_seconds())))
    return rows


class State:
    def __init__(self) -> None:
        self.section = 0
        self.selected = 0
        self.message = ""
        self.status = status_rows()
        self.confirm: tuple[str, list[str]] | None = None

    def actions(self) -> list[tuple[str, list[str] | None, bool]]:
        """(label, argv, needs_confirmation) for the current section."""
        name = SECTIONS[self.section]
        if name == "Update":
            return [
                ("Update the whole stack", ["plebian-os-update"], True),
                ("Update Kilix only", ["pleb", "update", "-y", "--no-restart"], True),
                ("Show update status", ["pleb", "status"], False),
            ]
        if name == "Session":
            return [
                ("Kiosk on", ["pleb", "kiosk", "on"], False),
                ("Kiosk off", ["pleb", "kiosk", "off"], False),
                ("Autologin on", ["pleb", "autologin", "on"], True),
                ("Autologin off", ["pleb", "autologin", "off"], True),
                ("Chrome settings", ["kilix-settings"], False),
            ]
        if name == "Power":
            # Shared with the kilix-tui desktop: one list of what "Shut down"
            # actually runs, however many surfaces offer it.
            return list(privileged.power_actions())
        if name == "Health":
            return [
                ("Run pleb doctor", ["pleb", "doctor"], False),
                ("Recovery guide", ["less", "/usr/local/share/doc/pleb/RECOVERY.md"], False),
            ]
        return []


def render(surface, state: State) -> None:
    height, width = surface.getmaxyx()
    surface.addstr(0, 0, "Plebian-OS Control"[: width - 1])
    tabs = "  ".join(
        f"{index + 1} {name}" + ("*" if index == state.section else "")
        for index, name in enumerate(SECTIONS))
    surface.addstr(1, 0, tabs[: width - 1])
    row = 3
    if state.confirm is not None:
        surface.addstr(row, 0, f"Confirm: {state.confirm[0]}"[: width - 1])
        surface.addstr(row + 1, 0, "y to proceed · any other key to cancel"[: width - 1])
        surface.addstr(height - 1, 0, "q quit"[: width - 1])
        return
    if SECTIONS[state.section] == "Status":
        for label, value in state.status:
            if row >= height - 2:
                break
            surface.addstr(row, 0, f"{label:<16}{value}"[: width - 1])
            row += 1
    else:
        for index, (label, _argv, needs) in enumerate(state.actions()):
            if row >= height - 4:
                break
            marker = ">" if index == state.selected else " "
            tag = "  (confirms)" if needs else ""
            surface.addstr(row, 0, f"{marker} {label}{tag}"[: width - 1])
            row += 1
    if state.message:
        row = min(row + 1, height - 3)
        for line in state.message.splitlines()[: max(0, height - row - 1)]:
            surface.addstr(row, 0, line[: width - 1])
            row += 1
    surface.addstr(height - 1, 0,
                   "1-5 section · ↑/↓ · Enter run · q quit"[: width - 1])


def handle(key: int, state: State) -> bool:
    if state.confirm is not None:
        label, argv = state.confirm
        state.confirm = None
        if key in (ord("y"), ord("Y")):
            state.message = f"$ {' '.join(argv)}\n" + _run(argv, timeout=600)
        else:
            state.message = f"cancelled: {label}"
        return True
    if keymap.is_quit(key):
        return False
    if ord("1") <= key <= ord("5"):
        state.section = key - ord("1")
        state.selected = 0
        state.message = ""
    elif step := keymap.direction(key):
        actions = state.actions()
        if actions:
            state.selected = max(0, min(len(actions) - 1, state.selected + step))
    elif key in keymap.SELECT:
        actions = state.actions()
        if actions and state.selected < len(actions):
            label, argv, needs = actions[state.selected]
            if argv is None:
                return True
            if needs:
                state.confirm = (label, argv)
            else:
                state.message = f"$ {' '.join(argv)}\n" + _run(argv, timeout=600)
    elif keymap.is_refresh(key):
        state.status = status_rows()
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if argv and argv[0] in ("--status", "-s"):
        for label, value in state.status:
            print(f"{label:<16}{value}")
        return 0
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
