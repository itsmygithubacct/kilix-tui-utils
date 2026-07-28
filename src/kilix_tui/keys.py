"""One keymap for every tool in this repository.

Thirteen utilities that each invent their own quit key would be thirteen things
to learn. These names are the vocabulary; a tool asks "was this a quit?" rather
than comparing key codes itself, so adding a binding once adds it everywhere.

The set deliberately matches what `kilix-settings` already uses, so a user who
knows the settings TUI can drive any of these without reading anything.
"""
from __future__ import annotations

QUIT = frozenset({ord("q"), ord("Q"), 27})          # q, Esc
HELP = frozenset({ord("?"), ord("h") - 96})          # ?, Ctrl-H
REFRESH = frozenset({ord("r"), ord("R"), ord("l") - 96})
UP = frozenset({ord("k"), 259})                      # k, KEY_UP
DOWN = frozenset({ord("j"), 258})                    # j, KEY_DOWN
LEFT = frozenset({ord("h"), 260})                    # h, KEY_LEFT
RIGHT = frozenset({ord("l"), 261})                   # l, KEY_RIGHT
SELECT = frozenset({ord("\n"), ord("\r"), ord(" "), 343})
PAGE_UP = frozenset({339})
PAGE_DOWN = frozenset({338})
HOME = frozenset({262, ord("g")})
END = frozenset({360, ord("G")})

# Shown in every tool's footer so the vocabulary is discoverable in place.
FOOTER = "↑/↓ move · Enter select · r refresh · ? help · q quit"


def is_quit(key: int) -> bool:
    return key in QUIT


def is_help(key: int) -> bool:
    return key in HELP


def is_refresh(key: int) -> bool:
    return key in REFRESH


def direction(key: int) -> int:
    """Return -1 for up, 1 for down, 0 otherwise."""
    if key in UP:
        return -1
    if key in DOWN:
        return 1
    return 0
