"""kilix-volume — output volume and sink selection.

Replaces the pulsemixer dependency the chrome volume item currently shells out
to. Supports both PipeWire and PulseAudio through whichever control command is
present, and degrades to a clear message rather than an exception when neither
is.

Bare invocation, --compact, --settings, and --full open the mixer. Screenshot
mode writes one frame and exits. get, set, mute, unmute, and sinks read or
change the default sink without opening the mixer.
"""
from __future__ import annotations

import curses
import os
import sys

_TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(os.path.dirname(_TOOL_DIR)), "src")
sys.path.insert(0, _TOOL_DIR)
sys.path.insert(0, _SRC)

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402
from kilix_volume import cli as volume_cli  # noqa: E402
from kilix_volume import control as backend  # noqa: E402

CONTROLS = backend.CONTROLS


def control() -> str | None:
    return backend.which_control()


def sinks() -> list[dict[str, object]]:
    try:
        found = backend.sinks()
    except backend.VolumeError:
        return []
    rows: list[dict[str, object]] = []
    for sink in found:
        target = sink.target or sink.name
        rows.append({
            "index": target,
            "name": target,
            "description": sink.description or sink.name or target,
            "volume": sink.percent,
            "muted": sink.muted,
            "default": sink.default,
        })
    return rows


def set_volume(name: str, percent: int) -> None:
    try:
        backend.set_target(name, percent)
    except backend.VolumeError:
        return


def toggle_mute(name: str) -> None:
    try:
        backend.toggle_mute(name)
    except backend.VolumeError:
        return


def make_default(name: str) -> None:
    try:
        backend.set_default(name)
    except backend.VolumeError:
        return


class State:
    def __init__(self, mode: str = "full") -> None:
        self.control = control()
        self.mode = mode
        self.sinks: list[dict[str, object]] = []
        self.selected = 0
        self.hits: dict[str, object] = {}
        self.refresh()

    def refresh(self) -> None:
        self.sinks = sinks() if self.control else []
        self.selected = min(self.selected, max(0, len(self.sinks) - 1))

    @property
    def active(self) -> dict[str, object] | None:
        return self.sinks[self.selected] if self.sinks else None


def render(surface, state: State) -> None:
    state.hits = {}
    if state.mode in ("compact", "settings"):
        render_popup(surface, state)
        return
    body = shell.draw(
        surface,
        title="Volume",
        sections=("Outputs",),
        summary=(
            f"{len(state.sinks)} outputs · {state.control}"
            if state.control else "No audio control command found"
        ),
        footer=(
            "←/→ volume · m mute · d default · r refresh · q quit"
            if state.control else "q quit"
        ),
        summary_role="muted" if state.control else "alert",
    )
    if state.control is None:
        shell.put(surface, body.top, body.left,
                  "No PulseAudio or PipeWire control command found.")
        shell.put(surface, body.top + 1, body.left,
                  "Install pulseaudio-utils (pactl) or wireplumber (wpctl).",
                  shell.tango.attr("muted"))
        return
    row = body.top
    for index, sink in enumerate(state.sinks):
        if row >= body.bottom:
            break
        selected = index == state.selected
        marker = "▶" if selected else " "
        star = "*" if sink.get("default") else " "
        level = int(sink["volume"])
        label = "muted" if sink["muted"] else f"{level:3d}%"
        shell.put(
            surface, row, body.left,
            f"{marker}{star} {str(sink['description']):<30.30} "
            f"{label} {proc.bar(level / 100, max(0, body.width - 46))}",
            shell.tango.attr("selected") if selected else 0,
        )
        state.hits.setdefault("sink_rows", {})[row] = index
        state.hits.setdefault("sliders", {})[row] = (
            body.left + 46, max(1, body.width - 46), index)
        row += 1


def render_popup(surface, state: State) -> None:
    settings = state.mode == "settings"
    body = shell.draw(
        surface,
        title="Volume settings" if settings else "Volume",
        sections=("Settings" if settings else "Output",),
        summary="Right-click menu" if settings else "Quick adjustment",
        footer=("click or ←/→ adjust · m mute · Enter full control · q quit"
                if not settings else
                "click checkbox · m mute · Enter full control · q quit"),
    )
    sink = state.active
    if sink is None:
        shell.put(surface, body.top, body.left, "No audio output is available.",
                  shell.tango.attr("alert"))
        return
    row = body.top
    description = str(sink.get("description") or sink.get("name") or "Output")
    shell.put(surface, row, body.left, description[:body.width],
              shell.tango.attr("selected"))
    row += 2
    muted = bool(sink["muted"])
    mute_text = f"[{'x' if muted else ' '}] Mute"
    shell.put(surface, row, body.left, mute_text,
              shell.tango.attr("alert") if muted else 0)
    state.hits["mute"] = (row, body.left, body.left + len(mute_text) - 1)
    row += 2
    if settings:
        label = "Open full volume control"
        shell.put(surface, row, body.left, label, shell.tango.attr("accent"))
        state.hits["open"] = (row, body.left, body.left + len(label) - 1)
        return
    level = int(sink["volume"])
    width = max(8, min(50, body.width - 8))
    label = f"{level:3d}%  "
    shell.put(surface, row, body.left, label)
    slider_left = body.left + len(label)
    shell.put(surface, row, slider_left, proc.bar(level / 100, width))
    state.hits["slider"] = (row, slider_left, width, state.selected)


def _hit(y: int, x: int, rect: object) -> bool:
    row, left, right = rect  # type: ignore[misc]
    return y == row and left <= x <= right


def mouse(state: State) -> None:
    try:
        _id, x, y, _z, buttons = curses.getmouse()
    except Exception:
        return
    clicked = (getattr(curses, "BUTTON1_PRESSED", 0)
               | getattr(curses, "BUTTON1_CLICKED", 0)
               | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0))
    if not buttons & clicked:
        return
    sink = state.active
    if sink is None:
        return
    if (rect := state.hits.get("mute")) and _hit(y, x, rect):
        toggle_mute(str(sink["name"])); state.refresh(); return
    if (rect := state.hits.get("open")) and _hit(y, x, rect):
        state.mode = "full"; return
    if slider := state.hits.get("slider"):
        row, left, width, index = slider  # type: ignore[misc]
        if y == row and left <= x < left + width:
            state.selected = int(index)
            sink = state.active
            if sink:
                level = round((x - left) * 100 / max(1, width - 1))
                set_volume(str(sink["name"]), level); state.refresh()
            return
    rows = state.hits.get("sink_rows", {})
    if y in rows:  # type: ignore[operator]
        state.selected = int(rows[y])  # type: ignore[index]
        sliders = state.hits.get("sliders", {})
        if y in sliders:  # type: ignore[operator]
            left, width, index = sliders[y]  # type: ignore[index]
            if left <= x < left + width:
                state.selected = int(index)
                sink = state.active
                if sink:
                    level = round((x - left) * 100 / max(1, width - 1))
                    set_volume(str(sink["name"]), level); state.refresh()


def handle(key: int, state: State) -> bool:
    if keymap.is_quit(key):
        return False
    if key == curses.KEY_MOUSE:
        mouse(state)
        return True
    sink = state.active
    if step := keymap.direction(key):
        if state.sinks and state.mode == "full":
            state.selected = max(0, min(len(state.sinks) - 1,
                                        state.selected + step))
    elif key in keymap.LEFT and sink:
        set_volume(str(sink["name"]), int(sink["volume"]) - 5); state.refresh()
    elif key in keymap.RIGHT and sink:
        set_volume(str(sink["name"]), int(sink["volume"]) + 5); state.refresh()
    elif key == ord("m") and sink:
        toggle_mute(str(sink["name"])); state.refresh()
    elif key in keymap.ENTER and state.mode != "full":
        state.mode = "full"
    elif key == ord("d") and sink and state.mode == "full":
        make_default(str(sink["name"])); state.refresh()
    elif keymap.is_refresh(key):
        state.refresh()
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if volume_cli.command_requested(argv):
        return volume_cli.main(argv)
    mode = "settings" if "--settings" in argv else (
        "compact" if "--compact" in argv else "full")
    state = State(mode)
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as output:
            output.write(app.render_to_text(render, state) + "\n")
        return 0

    return app.run(render, state, handle=handle, mouse=True)


if __name__ == "__main__":
    raise SystemExit(main())
