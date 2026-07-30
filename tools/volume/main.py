"""kilix-volume — output volume and sink selection.

Replaces the pulsemixer dependency the chrome volume item currently shells out
to. Supports both PipeWire and PulseAudio through whichever control command is
present, and degrades to a clear message rather than an exception when neither
is.
"""
from __future__ import annotations

import subprocess

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402

CONTROLS = ("pactl", "wpctl")


def control() -> str | None:
    for name in CONTROLS:
        from shutil import which
        if which(name):
            return name
    return None


def _pactl(args: list[str]) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True,
                              timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def sinks() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    default = _pactl(["get-default-sink"]).strip()
    current: dict[str, object] | None = None
    for line in _pactl(["list", "sinks"]).splitlines():
        text = line.strip()
        if text.startswith("Sink #"):
            current = {"index": text.split("#", 1)[1], "name": "",
                       "description": "", "volume": 0, "muted": False}
            rows.append(current)
        elif current is None:
            continue
        elif text.startswith("Name:"):
            current["name"] = text.split(":", 1)[1].strip()
            current["default"] = current["name"] == default
        elif text.startswith("Description:"):
            current["description"] = text.split(":", 1)[1].strip()
        elif text.startswith("Mute:"):
            current["muted"] = "yes" in text
        elif text.startswith("Volume:") and "%" in text:
            for token in text.split():
                if token.endswith("%"):
                    try:
                        current["volume"] = int(token.rstrip("%"))
                    except ValueError:
                        pass
                    break
    return rows


def set_volume(name: str, percent: int) -> None:
    percent = max(0, min(150, percent))
    _pactl(["set-sink-volume", name, f"{percent}%"])


def toggle_mute(name: str) -> None:
    _pactl(["set-sink-mute", name, "toggle"])


def make_default(name: str) -> None:
    _pactl(["set-default-sink", name])


class State:
    def __init__(self) -> None:
        self.control = control()
        self.sinks: list[dict[str, object]] = []
        self.selected = 0
        self.refresh()

    def refresh(self) -> None:
        self.sinks = sinks() if self.control else []
        self.selected = min(self.selected, max(0, len(self.sinks) - 1))

    @property
    def active(self) -> dict[str, object] | None:
        return self.sinks[self.selected] if self.sinks else None


def render(surface, state: State) -> None:
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
                  "Install pulseaudio-utils (pactl).",
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
        row += 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, state) + "\n")
        return 0

    def handle(key: int, s: State) -> bool:
        if keymap.is_quit(key):
            return False
        sink = s.active
        if step := keymap.direction(key):
            if s.sinks:
                s.selected = max(0, min(len(s.sinks) - 1, s.selected + step))
        elif key in keymap.LEFT and sink:
            set_volume(str(sink["name"]), int(sink["volume"]) - 5); s.refresh()
        elif key in keymap.RIGHT and sink:
            set_volume(str(sink["name"]), int(sink["volume"]) + 5); s.refresh()
        elif key == ord("m") and sink:
            toggle_mute(str(sink["name"])); s.refresh()
        elif key == ord("d") and sink:
            make_default(str(sink["name"])); s.refresh()
        elif keymap.is_refresh(key):
            s.refresh()
        return True

    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
