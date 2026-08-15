"""kilix-tui — the text-native desktop for the Plebian-OS stack.

An index and a power switch over the utilities this repository ships: every
entry launches a tool that already exists, in place or in a Kilix page. The
canonical Tango-coloured text shell is the default everywhere. The optional
Kitty pixel rendering remains available only through ``--graphics``.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from kilix_desk import desk  # noqa: E402
from kilix_tui import app  # noqa: E402


FOCUSED_APPS: dict[str, tuple[str, tuple[str, ...], dict[str, str]]] = {
    "system": (
        "System Center",
        ("Machine",),
        {
            "overview": "",
            "cpu": "CPU",
            "memory": "Memory",
            "thermal": "Temperatures",
            "storage": "Disk",
            "network": "Network",
            "audio": "Volume",
            "packages": "Packages",
            "cameras": "Cameras",
            "virtualbox": "VirtualBox VPN",
        },
    ),
    "settings": (
        "Kilix Settings",
        ("System",),
        {
            "overview": "",
            "audio": "Audio settings",
            "display": "Screen size",
            "desktop": "Default desktop",
            "voice": "Voice status",
        },
    ),
    "software": (
        "Software Center",
        ("Programs", "Software"),
        {"browse": "", "details": "", "install": ""},
    ),
    "session": (
        "Session Center",
        ("Session",),
        {
            "overview": "",
            "panes": "Switcher",
            "pty": "PTY sessions",
            "logs": "Session logs",
            "remote-sessions": "Attach to a session",
        },
    ),
    "voice": (
        "Voice Studio",
        ("Programs", "Voice"),
        {
            "overview": "",
            "read-aloud": "Read aloud",
            "dictation": "Dictation",
            "models": "Model store",
            "settings": "Voice settings",
            "status": "Voice status",
            "doctor": "Voice doctor",
        },
    ),
}


def _option_value(argv: list[str], option: str) -> str | None:
    if option not in argv:
        return None
    index = argv.index(option)
    if index + 1 >= len(argv):
        raise ValueError(f"{option} needs a value")
    return argv[index + 1]


def _focused_state(argv: list[str]) -> tuple[desk.State, str | None, str | None]:
    """Build either the full desktop or one catalog-facing focused center."""
    app_id = _option_value(argv, "--app")
    action = _option_value(argv, "--action")
    action_input = None
    if app_id is None:
        if action is not None:
            raise ValueError("--action requires --app")
        return desk.State(), None, None
    definition = FOCUSED_APPS.get(app_id)
    if definition is None:
        raise ValueError(f"unknown focused app {app_id!r}")
    title, root_path, actions = definition
    if action is not None:
        if action not in actions:
            raise ValueError(f"{app_id}: unknown action {action!r}")
        action_index = argv.index("--action")
        if action_index + 2 < len(argv) and not argv[action_index + 2].startswith("--"):
            action_input = argv[action_index + 2]
    return (
        desk.State(root_path=root_path, application_name=title),
        action,
        action_input,
    )


def _apply_action(
    state: desk.State,
    app_id: str | None,
    action: str | None,
    action_input: str | None,
) -> None:
    if app_id is None or action is None:
        return
    _title, _path, actions = FOCUSED_APPS[app_id]
    label = actions[action]
    rows = state.entries()
    if app_id == "software" and action in ("details", "install"):
        if not action_input:
            raise ValueError(f"{app_id} {action} needs a catalog ID")
        wanted = action_input.casefold()
        matches = [
            (index, row)
            for index, row in enumerate(rows)
            if row.argv and row.argv[-1].casefold() == wanted
        ]
        if not matches:
            state.message = f"No catalog entry matches {action_input!r}."
            return
        state.selected, row = matches[0]
        if action == "install" and row.argv is not None:
            state.confirm = (f"Install {row.label}", row.argv)
        return
    if not label:
        return
    for index, row in enumerate(rows):
        if row.label == label:
            state.selected = index
            return
    state.message = f"{label} is not available on this machine."


def _graphics_wanted(argv: list[str]) -> bool:
    if "--text" in argv:
        return False
    return "--graphics" in argv


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        state, action, action_input = _focused_state(argv)
        app_id = _option_value(argv, "--app")
        _apply_action(state, app_id, action, action_input)
    except ValueError as error:
        print(f"kilix-tui: {error}", file=sys.stderr)
        return 2
    if argv and argv[0] in ("--status", "-s"):
        for label, value in state.status:
            print(f"{label:<18}{value}")
        return 0
    if "--section" in argv:
        index = argv.index("--section")
        if index + 1 < len(argv) and argv[index + 1] in desk.SECTIONS:
            state.section = desk.SECTIONS.index(argv[index + 1])
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(desk.render, state) + "\n")
        return 0
    if _graphics_wanted(argv):
        from kilix_desk import graphics, gui
        try:
            return gui.run(state)
        except graphics.GraphicsUnavailable as error:
            if "--graphics" in argv:
                print(f"kilix-tui: graphics unavailable: {error}",
                      file=sys.stderr)
                return 1
            # The floor: fall through to the text session.
    # The idle screensaver rides the shared loop (F-SAVER): after the
    # configured quiet spell the desk hands the terminal to
    # `kilix screensaver`, and any key is the way back.
    return app.run(desk.render, state, handle=desk.handle, mouse=True,
                   idle_after=desk.idle_saver_seconds(),
                   on_idle=desk.start_screensaver)


if __name__ == "__main__":
    raise SystemExit(main())
