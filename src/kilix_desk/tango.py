"""The desktop's Tango palette, for pixels and for plain text.

The desktop wears Kilix's own colours — Tango blues, reds, whites and greys —
rather than the theatrical panel look the smaller tools opted into. One module
holds both renderings of that decision: RGB triples for the pixel renderer,
and curses attributes for the text fallback, resolved lazily the same way
`theme.panel_pairs` resolves so headless renders still get distinct,
assertable attributes.
"""
from __future__ import annotations

# Tango, by role. Blues carry structure and selection, red is reserved for
# power and refusal, whites speak, greys support.
BG_TOP = (38, 42, 48)
BG_BOTTOM = (24, 27, 32)
HEADER = (28, 32, 38)
CARD = (40, 45, 52)
CARD_EDGE = (60, 66, 74)
CARD_SHADOW = (16, 18, 22)
ROW_ALT = (46, 51, 59)

BLUE_DEEP = (32, 74, 135)      # Tango sky blue 3
BLUE = (52, 101, 164)          # Tango sky blue 2
BLUE_BRIGHT = (114, 159, 207)  # Tango sky blue 1
RED_DEEP = (164, 0, 0)         # Tango scarlet red 3
RED = (204, 0, 0)              # Tango scarlet red 2
RED_BRIGHT = (239, 41, 41)     # Tango scarlet red 1
WHITE = (238, 238, 236)        # Tango aluminium 1
SILVER = (211, 215, 207)       # Tango aluminium 2
GREY = (136, 138, 133)         # Tango aluminium 4
GREY_DARK = (85, 87, 83)       # Tango aluminium 5

# ── text-mode attributes ─────────────────────────────────────────────────────
#
# Five roles are all the text layout needs. Pairs are allocated on first use
# and only when curses is actually running; a headless render gets stable
# synthetic values instead, so `attr_shape()` can assert the layout without a
# terminal — the same bargain `theme.panel_pairs` makes for the panel look.

_ATTRS: dict[str, int] | None = None
_PAIR_BASE = 48    # clear of the panel look's 32.. block


def _resolve() -> dict[str, int]:
    global _ATTRS
    if _ATTRS is not None:
        return _ATTRS
    try:
        import curses
        if not curses.has_colors():
            raise RuntimeError("no colours")
        attrs: dict[str, int] = {}
        pairs = (
            ("title", curses.COLOR_WHITE, -1),
            ("accent", curses.COLOR_BLUE, -1),
            ("alert", curses.COLOR_RED, -1),
            ("muted", curses.COLOR_WHITE, -1),
            ("selected", curses.COLOR_WHITE, curses.COLOR_BLUE),
            ("danger", curses.COLOR_WHITE, curses.COLOR_RED),
        )
        try:
            curses.use_default_colors()
            background = -1
        except Exception:
            background = curses.COLOR_BLACK
        for index, (role, fg, bg) in enumerate(pairs):
            curses.init_pair(_PAIR_BASE + index, fg,
                             bg if bg != -1 else background)
            attrs[role] = curses.color_pair(_PAIR_BASE + index)
        attrs["title"] |= curses.A_BOLD
        attrs["muted"] |= curses.A_DIM
        attrs["selected"] |= curses.A_BOLD
        attrs["danger"] |= curses.A_BOLD
        _ATTRS = attrs
    except Exception:
        # Headless, monochrome, or curses refused: stable synthetic values
        # that only have to differ from each other and from zero.
        _ATTRS = {
            "title": 101, "accent": 102, "alert": 103,
            "muted": 104, "selected": 105, "danger": 106,
        }
    return _ATTRS


def attr(role: str) -> int:
    """One text attribute for `role`; synthetic but distinct when headless."""
    return _resolve().get(role, 0)


def reset() -> None:
    """Forget cached attributes. For tests that re-enter curses."""
    global _ATTRS
    _ATTRS = None
