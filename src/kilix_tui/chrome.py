"""One composed page, so every tool that adopts the panel look is a sibling.

`app.py` already makes the bargain that a tool supplies `render` and gets the
event loop free. This is the same bargain for layout: a tool describes its
sections and draws into `content_box()`, and the spine, the elbow, the title,
the rhythm bar and the identifiers all happen without it.

The layout degrades in one step rather than many. Below the size where a spine
would cost more than it is worth, the spine and the elbow are dropped and the
tool gets nearly the whole surface, still titled and still styled. That
threshold matters: this look is decorative and a terminal has few cells, so a
chrome that ate half of an 80-column pane would have made the tool worse.
"""
from __future__ import annotations

from typing import Any, Sequence

from . import panel, theme

# Below either of these the spine is dropped. 60 columns is roughly where a
# 14-column spine stops being affordable next to a useful content well.
MIN_SPINE_COLUMNS = 60
MIN_SPINE_ROWS = 14


class Page:
    """Spine on the left, title upper right, content well in the middle."""

    def __init__(
        self,
        title: str,
        sections: Sequence[str] = (),
        *,
        spine_width: int = 14,
        seed: int = 7,
        node: str = "",
    ) -> None:
        self.title = title
        self.sections = list(sections)
        self.spine_width = spine_width
        self.seed = seed
        self.node = node
        self._height = 24
        self._width = 80

    # ── geometry ─────────────────────────────────────────────────────────────

    def measure(self, surface: Any) -> None:
        """Latch the surface size; call once at the top of a render."""
        try:
            self._height, self._width = surface.getmaxyx()
        except Exception:
            self._height, self._width = 24, 80

    @property
    def spined(self) -> bool:
        """False when the surface is too small to spend cells on chrome."""
        return (
            self.spine_width > 0
            and self._width >= MIN_SPINE_COLUMNS
            and self._height >= MIN_SPINE_ROWS
        )

    def content_box(self) -> tuple[int, int, int, int]:
        """(top, left, height, width) of the well a tool may draw into."""
        if not self.spined:
            # Title row, then everything down to the footer.
            return 1, 0, max(0, self._height - 2), self._width
        left = self.spine_width + 1
        top = 4                       # title, then the two-row elbow, then a gap
        height = max(0, self._height - top - 2)
        return top, left, height, max(0, self._width - left)

    # ── drawing ──────────────────────────────────────────────────────────────

    def render(
        self,
        surface: Any,
        active_section: int = 0,
        *,
        footer: str = "",
        status: str = "",
    ) -> None:
        self.measure(surface)
        height, width = self._height, self._width
        panel.title(surface, 0, width - 1, self.title)

        if not self.spined:
            if status:
                panel._put(
                    surface, 0, 0, status[: max(0, width - len(self.title) - 2)],
                    theme.panel_attr("secondary", on_void=True),
                )
            if footer:
                panel._put(
                    surface, height - 1, 0, footer[:width],
                    theme.panel_attr("quaternary", on_void=True),
                )
            return

        if status:
            panel._put(
                surface, 0, 0, status[: max(0, width - len(self.title) - 2)],
                theme.panel_attr("secondary", on_void=True),
            )

        # The elbow sweeps out of the spine's head into a solid bar, and the
        # bar then breaks into segments rather than running flat to the edge —
        # two stacked runs, which is what the reference does and what stops the
        # top of the screen reading as a title bar.
        sweep = min(width, self.spine_width + 22)
        panel.elbow(
            surface, 1, 0, 3, sweep, "quaternary",
            corner="tl", thickness=2, stem=self.spine_width, cap=False,
        )
        rest = width - sweep - 1
        if rest > 4:
            panel.bar(surface, 1, sweep + 1, rest, self._segments(rest, 0))
            panel.bar(surface, 2, sweep + 1, rest, self._segments(rest, 1))
        else:
            panel.bar(surface, 1, sweep + 1, max(0, rest))

        # Spine sections stack below the elbow's leg.
        top, _, well_height, _ = self.content_box()
        self._render_spine(surface, top, well_height, active_section)

        # Rhythm bar and footer along the bottom.
        panel.bar(surface, height - 2, self.spine_width + 1, width - self.spine_width - 1)
        # The node tag shares the footer row and is pure decoration, so it is
        # what gives way when the two will not both fit — a truncated key hint
        # costs the user something, a missing identifier does not.
        room = width - self.spine_width - 1
        show_node = bool(self.node) and room - len(footer) >= len(self.node) + 2
        if footer:
            budget = room - (len(self.node) + 2 if show_node else 0)
            panel._put(
                surface, height - 1, self.spine_width + 1, footer[: max(0, budget)],
                theme.panel_attr("quaternary", on_void=True),
            )
        if show_node:
            panel.title(surface, height - 1, width - 1, self.node)

    def _segments(self, width: int, row: int) -> list[tuple[int, str]]:
        """Uneven segment runs for one bar row, stable for a given width.

        The two rows are given different weights on purpose: identical runs
        stacked on top of each other read as a table, not as rhythm.
        """
        weights = ((3, 11, 6, 2, 8), (7, 4, 13, 3, 5))[row % 2]
        cycle = theme.PANEL_CYCLE
        total = sum(weights)
        out = []
        for index, weight in enumerate(weights):
            cells = max(1, round((width - len(weights)) * weight / total))
            out.append((cells, cycle[(index + row) % len(cycle)]))
        return out

    def _render_spine(
        self, surface: Any, top: int, height: int, active: int,
    ) -> None:
        if height <= 0:
            return
        count = max(1, len(self.sections))
        # One block per section, sharing the height, each with a black gap under
        # it. A section that would be less than two rows is not worth drawing.
        each = max(2, height // count)
        row = top
        for index, label in enumerate(self.sections):
            if row + 2 > top + height:
                break
            block_height = min(each - 1, top + height - row)
            if block_height < 2:
                break
            colour = theme.PANEL_CYCLE[index % len(theme.PANEL_CYCLE)]
            is_active = index == active
            # The active section must be readable without the palette. Swapping
            # its fill to the primary colour is the whole point of the look, but
            # colour alone would leave a 16-colour terminal — or anyone who
            # cannot distinguish these hues — unable to tell which scope is on.
            text = f"▶{label}" if is_active else f" {label}"
            panel.block(
                surface, row, 0, block_height, self.spine_width, colour,
                label=text[: self.spine_width - 2],
                ident=panel.ident(self.seed, index + 2),
                round_corners=("tl", "bl"),
                active=is_active,
            )
            row += block_height + 1
        # Whatever is left of the spine becomes texture rather than a gap.
        if row < top + height - 1:
            panel.block(
                surface, row, 0, top + height - row, self.spine_width, "tertiary",
                ident=panel.ident(self.seed, len(self.sections) + 2),
                round_corners=("tl", "bl"),
            )
