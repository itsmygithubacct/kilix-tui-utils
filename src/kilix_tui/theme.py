"""Colours and metrics, read from the one shared settings file.

Every Kilix component — the terminal, both desktop providers, Pleb, and
Plebian-OS — already reads `~/.local/gpu_terminal/settings.conf`. These tools
join that contract rather than inventing a second place to configure them, so a
change made in any settings interface reaches all of them.

The SDK is imported when a Kilix checkout is reachable and its absence is not
fatal: a tool run from a bare checkout, over SSH, or on a machine without Kilix
installed still starts with the built-in defaults.
"""
from __future__ import annotations

import os
from typing import Any

_SDK: Any | None = None


def _sdk() -> Any | None:
    """Return `kilix_sdk.settings`, or None when Kilix is not reachable."""
    global _SDK
    if _SDK is not None:
        return _SDK or None
    import importlib
    import sys

    source_home = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or os.path.join(
        os.path.expanduser("~"), "gpu_terminal")
    candidates = [
        os.environ.get("KILIX_HOME", ""),
        os.path.join(os.path.abspath(os.path.expanduser(source_home)), "kilix"),
    ]
    for home in candidates:
        config = os.path.join(home, "config") if home else ""
        if config and os.path.isdir(os.path.join(config, "kilix_sdk")):
            if config not in sys.path:
                sys.path.insert(0, config)
            try:
                _SDK = importlib.import_module("kilix_sdk.settings")
                return _SDK
            except Exception:
                break
    _SDK = False  # type: ignore[assignment]
    return None


def setting(key: str, default: str) -> str:
    """Read one shared setting, falling back when Kilix is unavailable."""
    sdk = _sdk()
    if sdk is None:
        return os.environ.get(key, default)
    try:
        return sdk.load().get(key, default)
    except Exception:
        return default


# Tango-ish palette matching Kilix's own chrome. Curses colour numbers; tools
# that draw into a framebuffer use the RGB triples instead.
FG = 7
ACCENT = 4
WARN = 3
ALERT = 1
MUTED = 8

RGB = {
    "fg": (0xD3, 0xD7, 0xCF),
    "bg": (0x2E, 0x34, 0x36),
    "accent": (0x34, 0x65, 0xA4),
    "warn": (0xC4, 0xA0, 0x00),
    "alert": (0xCC, 0x00, 0x00),
    "muted": (0x55, 0x57, 0x53),
}


# ── the panel look ───────────────────────────────────────────────────────────
#
# A second, deliberately theatrical palette for tools that opt into `panel.py`:
# flat colour blocks on black, no white, no grey, no gradients, and no borders —
# separation is done with black gaps rather than lines. Anyone who recognises
# the influence will recognise it immediately; nothing here claims it.
#
# These are xterm-256 indices, so a tool must check `panel_capable()` before
# using them. Everything degrades to the Tango palette above when it is false,
# which is what keeps these tools usable over `ssh` from a 8- or 16-colour
# terminal.

PANEL = {
    "primary": 214,      # orange — titles and the active block
    "secondary": 221,    # amber
    "tertiary": 182,     # mauve
    "quaternary": 147,   # periwinkle
    "alert": 167,        # salmon
    "ink": 0,            # text drawn *on* a panel is always black
    "void": 0,           # and the background is always black
}

PANEL_RGB = {
    "primary": (0xFF, 0x99, 0x00),
    "secondary": (0xFF, 0xCC, 0x66),
    "tertiary": (0xCC, 0x99, 0xCC),
    "quaternary": (0x99, 0x99, 0xFF),
    "alert": (0xCC, 0x66, 0x66),
    "ink": (0x00, 0x00, 0x00),
    "void": (0x00, 0x00, 0x00),
}

# The order the spine cycles through when a tool does not name a colour per
# section. Primary is held back for the active section so it always stands out.
PANEL_CYCLE = ("quaternary", "tertiary", "alert", "secondary")

_PAIRS: dict[str, int] | None = None


def panel_capable() -> bool:
    """True when the terminal can render the panel palette.

    False for a 16-colour terminal and whenever curses has not been started —
    the headless render path included, so a `--screenshot` is plain text and
    the text-only tests stay valid.

    `KILIX_PANEL` overrides the detection in both directions. `0` is the escape
    hatch for a terminal that claims 256 colours and lies. `1` is what makes the
    look testable at all: the fills are spaces, so without forcing the palette
    on there would be nothing in a headless render to assert a block against.
    """
    forced = os.environ.get("KILIX_PANEL")
    if forced == "0":
        return False
    if forced == "1":
        return True
    try:
        import curses
        return bool(curses.has_colors()) and curses.COLORS >= 256
    except Exception:
        return False


def panel_pairs() -> dict[str, int]:
    """Return role -> curses attribute for drawing on a panel.

    Two attributes per colour: `<role>` fills a block with that colour and
    writes black text into it, `<role>_on_void` writes that colour as text on
    the black background. Returns zeros when the terminal cannot do it, so
    callers never branch — they just get no styling.
    """
    global _PAIRS
    if _PAIRS is not None:
        return _PAIRS
    if not panel_capable():
        _PAIRS = {}
        return _PAIRS

    roles = [role for role in PANEL if role not in ("ink", "void")]
    pairs: dict[str, int] = {}
    index = 32  # leave the low pairs to whatever a tool already allocated
    try:
        import curses
        for role in roles:
            curses.init_pair(index, PANEL["ink"], PANEL[role])
            pairs[role] = curses.color_pair(index)
            curses.init_pair(index + 1, PANEL[role], PANEL["void"])
            pairs[f"{role}_on_void"] = curses.color_pair(index + 1)
            index += 2
    except Exception:
        # No curses, or it refused the pairs: hand back stable synthetic values
        # so a headless render still distinguishes the roles. They are never
        # sent to a terminal — nothing is drawing — they only have to differ.
        pairs = {}
        for offset, role in enumerate(roles):
            pairs[role] = 1 + offset * 2
            pairs[f"{role}_on_void"] = 2 + offset * 2
    _PAIRS = pairs
    return pairs


def reset_panel_pairs() -> None:
    """Forget the cached pairs. For tests that flip `KILIX_PANEL`."""
    global _PAIRS
    _PAIRS = None


def panel_attr(role: str, *, on_void: bool = False, bold: bool = False) -> int:
    """One attribute for `role`, or 0 when the panel palette is unavailable."""
    pairs = panel_pairs()
    if not pairs:
        return 0
    attr = pairs.get(f"{role}_on_void" if on_void else role, 0)
    if bold:
        try:
            import curses
            attr |= curses.A_BOLD
        except Exception:
            pass
    return attr
