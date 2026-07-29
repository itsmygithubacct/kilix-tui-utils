"""kilix-tui — the text-native desktop for the Plebian-OS stack.

An index and a power switch over the utilities this repository ships: every
entry launches a tool that already exists, in place or in a Kilix page. Two
renderings of one desktop: Tango-themed pixels over the Kitty graphics
protocol where the terminal can carry them, and Tango-coloured text
everywhere else — `ssh`, `tmux`, a bare console. `--text` and `--graphics`
pin a mode; by default the terminal decides.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from kilix_desk import desk  # noqa: E402
from kilix_tui import app  # noqa: E402


def _graphics_wanted(argv: list[str]) -> bool:
    if "--text" in argv:
        return False
    if "--graphics" in argv:
        return True
    if os.environ.get("KILIX_TUI_GRAPHICS") == "0":
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    from kilix_desk import graphics
    return graphics.kitty_graphics_likely() and graphics.available()[0]


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
    return app.run(desk.render, state, handle=desk.handle, mouse=True)


if __name__ == "__main__":
    raise SystemExit(main())
