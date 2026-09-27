"""One keymap for every tool in this repository.

Thirteen utilities that each invent their own quit key would be thirteen things
to learn. These names are the vocabulary; a tool asks "was this a quit?" rather
than comparing key codes itself, so adding a binding once adds it everywhere.

The set deliberately matches what `kilix-settings` already uses, so a user who
knows the settings TUI can drive any of these without reading anything.

`BINDINGS` is the one description of what each key does. The footer line and
the `?` overlay are both rendered from it, so a key can never be advertised in
one place and missing from the other — the failure that makes a TUI feel
untrustworthy.
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
FILTER = frozenset({ord("/")})                       # incremental filter
BACKSPACE = frozenset({263, 127, 8})

ESCAPE = 27
ENTER = frozenset({ord("\n"), ord("\r")})


# (keys as shown, what it does, the compact form for the footer or "")
BINDINGS: tuple[tuple[str, str, str], ...] = (
    ("↑ ↓  j k", "move the cursor", "↑↓ move"),
    ("→  Enter", "open what is selected", "→ open"),
    ("←  Esc", "go back one level", "← back"),
    ("/", "filter this list as you type", "/ filter"),
    ("!", "type a command and run it", "! run"),
    ("?", "show these keys", "? keys"),
    ("q", "quit", "q quit"),
    ("t", "on a game row: turn it on or off for the stack", ""),
    ("p", "pin the selected entry to Home, or unpin it", ""),
    ("1 – 6", "jump straight to a section", ""),
    ("PgUp PgDn", "move a page at a time", ""),
    ("g  G", "first entry, last entry", ""),
    ("r", "refresh what is on screen", ""),
    ("Tab", "next section", ""),
    ("mouse", "click to select, click again to open, wheel to scroll", ""),
)

SEPARATOR = " · "


def footer(width: int = 0, extra: str = "") -> str:
    """The key line for the last row, fitted to the terminal.

    A key line that runs off the edge is worse than a short one: the keys at
    the end are exactly the ones a stuck user needs, and `q` is last. So the
    line drops bindings from the *middle* outwards until it fits, and quit
    survives every trim.
    """
    parts = [short for _keys, _what, short in BINDINGS if short]
    if extra:
        parts.insert(len(parts) - 1, extra)
    if width <= 0:
        return SEPARATOR.join(parts)
    while len(parts) > 1 and len(SEPARATOR.join(parts)) > width:
        del parts[max(0, len(parts) - 2)]        # never the last one: q quit
    line = SEPARATOR.join(parts)
    return line if len(line) <= width else line[:width]


# Kept as a constant for tools that want the plain line with no additions.
FOOTER = "↑/↓ move · Enter select · r refresh · ? help · q quit"


def help_rows() -> tuple[tuple[str, str], ...]:
    """Every binding, for the `?` overlay."""
    return tuple((keys, what) for keys, what, _short in BINDINGS)


def is_quit(key: int) -> bool:
    return key in QUIT


def is_help(key: int) -> bool:
    return key in HELP


def is_help_char(key: int) -> bool:
    """Just `?`, for the shared loop.

    `HELP` also carries Ctrl-H, which arrives as 8 — the same code many
    terminals send for Backspace, and Backspace is "go up a directory" in the
    file manager. The central interception must not swallow that, so it matches
    the one key that means help and nothing else.
    """
    return key == ord("?")


def is_refresh(key: int) -> bool:
    return key in REFRESH


def is_filter(key: int) -> bool:
    return key in FILTER


def is_text(key: int) -> bool:
    """A printable character a filter can accept."""
    return 32 <= key <= 126


def direction(key: int) -> int:
    """Return -1 for up, 1 for down, 0 otherwise."""
    if key in UP:
        return -1
    if key in DOWN:
        return 1
    return 0
