"""kilix-tui — the text-native desktop for the Plebian-OS stack.

An index and a power switch over the utilities this repository ships: every
entry launches a tool that already exists, in place or in a Kilix page. Runs
anywhere text works — a Kilix pane, `ssh`, `tmux`, a bare console — and wears
the panel look wherever the terminal can carry it.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from kilix_desk import desk  # noqa: E402
from kilix_tui import app  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = desk.State()
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
    return app.run(desk.render, state, handle=desk.handle)


if __name__ == "__main__":
    raise SystemExit(main())
