"""The desktop's Tango palette, for pixels and for plain text.

The desktop wears Kilix's own colours — Tango blues, reds, whites and greys.
One module holds both renderings of that decision: RGB triples for the pixel
renderer and curses attributes for the text fallback.

F-FLAVOR, recorded: the look stays one deliberate design, but the accent hue
is now a choice. A flavor swaps exactly the structural/selection ramp — the
three `BLUE_*` names and the curses colour behind `accent` and `selected` —
and nothing else, because red stays reserved for power and refusal in every
flavor and the whites and greys are the furniture. `KILIX_TUI_FLAVOR` in the
shared `settings.conf` (or the environment, like every `theme.setting` knob)
selects one for every session; System ▸ Palette tries one on right now.
"""
from __future__ import annotations

# Tango, by role. The accent ramp carries structure and selection, red is
# reserved for power and refusal, whites speak, greys support.
BG_TOP = (38, 42, 48)
BG_BOTTOM = (24, 27, 32)
HEADER = (28, 32, 38)
CARD = (40, 45, 52)
CARD_EDGE = (60, 66, 74)
CARD_SHADOW = (16, 18, 22)
ROW_ALT = (46, 51, 59)

BLUE_DEEP = (32, 74, 135)      # Tango sky blue 3 — `apply()` re-aims all three
BLUE = (52, 101, 164)          # Tango sky blue 2
BLUE_BRIGHT = (114, 159, 207)  # Tango sky blue 1
RED_DEEP = (164, 0, 0)         # Tango scarlet red 3
RED = (204, 0, 0)              # Tango scarlet red 2
RED_BRIGHT = (239, 41, 41)     # Tango scarlet red 1
WHITE = (238, 238, 236)        # Tango aluminium 1
SILVER = (211, 215, 207)       # Tango aluminium 2
GREY = (136, 138, 133)         # Tango aluminium 4
GREY_DARK = (85, 87, 83)       # Tango aluminium 5

# ── flavors ──────────────────────────────────────────────────────────────────
#
# Every ramp is a real Tango hue (shades 3, 2, 1), so any flavor still reads
# as this desktop. `pair` is the curses colour that carries the same role on
# a text terminal, named so the table works without importing curses here.

FLAVORS: dict[str, dict[str, object]] = {
    "tango": {"label": "Tango", "note": "sky blue, the default",
              "deep": (32, 74, 135), "mid": (52, 101, 164),
              "bright": (114, 159, 207), "pair": "COLOR_BLUE"},
    "chameleon": {"label": "Chameleon", "note": "Tango green",
                  "deep": (78, 154, 6), "mid": (115, 210, 22),
                  "bright": (138, 226, 52), "pair": "COLOR_GREEN"},
    "plum": {"label": "Plum", "note": "Tango purple",
             "deep": (92, 53, 102), "mid": (117, 80, 123),
             "bright": (173, 127, 168), "pair": "COLOR_MAGENTA"},
    "amber": {"label": "Amber", "note": "Tango orange",
              "deep": (206, 92, 0), "mid": (245, 121, 0),
              "bright": (252, 175, 62), "pair": "COLOR_YELLOW"},
}
DEFAULT_FLAVOR = "tango"
FLAVOR = DEFAULT_FLAVOR


def flavor_setting() -> str:
    """The configured flavor name, from the one shared settings file.

    Unknown names degrade to the default rather than to an error: a palette
    is convenience, and a typo in `settings.conf` must never cost a desktop.
    """
    try:
        from kilix_tui import theme
        name = str(theme.setting("KILIX_TUI_FLAVOR", DEFAULT_FLAVOR))
    except Exception:
        name = DEFAULT_FLAVOR
    name = name.strip().lower()
    return name if name in FLAVORS else DEFAULT_FLAVOR


def apply(name: str | None = None) -> str:
    """Wear one flavor: re-aim the accent ramp both renderers read.

    With no name (or an unknown one) the shared setting decides. Cached text
    attributes are forgotten so a running session re-pairs `accent` and
    `selected` on its next frame. Returns the name actually applied.
    """
    global FLAVOR, BLUE_DEEP, BLUE, BLUE_BRIGHT
    FLAVOR = name if name in FLAVORS else flavor_setting()
    ramp = FLAVORS[FLAVOR]
    BLUE_DEEP, BLUE, BLUE_BRIGHT = ramp["deep"], ramp["mid"], ramp["bright"]
    reset()
    return FLAVOR

# ── text-mode attributes ─────────────────────────────────────────────────────
#
# Five roles are all the text layout needs. Pairs are allocated on first use
# and only when curses is actually running; a headless render gets stable
# synthetic values instead, so `attr_shape()` can assert the layout without a
# terminal.

_ATTRS: dict[str, int] | None = None
_PAIR_BASE = 16


def _resolve() -> dict[str, int]:
    global _ATTRS
    if _ATTRS is not None:
        return _ATTRS
    try:
        import curses
        if not curses.has_colors():
            raise RuntimeError("no colours")
        attrs: dict[str, int] = {}
        accent = getattr(curses, str(FLAVORS[FLAVOR]["pair"]),
                         curses.COLOR_BLUE)
        pairs = (
            ("title", curses.COLOR_WHITE, -1),
            ("accent", accent, -1),
            ("alert", curses.COLOR_RED, -1),
            ("muted", curses.COLOR_WHITE, -1),
            ("selected", curses.COLOR_WHITE, accent),
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


# The configured flavor, worn from the first frame. `theme.setting` absorbs
# every way this can fail — no Kilix, no settings file — into the default.
apply()
