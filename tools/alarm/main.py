#!/usr/bin/env python3
"""kilix-alarm — set an alarm and keep this window open until it finishes.

The style is one of four: a flashing white screen, a low long quiet hum,
a loud alert beep, or an alarm clock that can snooze. The choice is saved.
Quitting drops an alarm that has not rung yet.
"""
from __future__ import annotations

import curses
import os
import sys

_TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(os.path.dirname(_TOOL_DIR)), "src")
sys.path.insert(0, _TOOL_DIR)
sys.path.insert(0, _SRC)

from kilix_alarm.model import (  # noqa: E402
    BLURBS,
    LABELS,
    STYLES,
    Alarm,
    AlarmError,
    load_settings,
)
from kilix_alarm.sound import Player, wav_bytes  # noqa: E402
from kilix_tui import app, keys as keymap, shell  # noqa: E402

_FLASH: int | None = None


def flash_attr() -> int:
    """White fill. Headless renders get a stable nonzero stand-in."""
    global _FLASH
    if _FLASH is not None:
        return _FLASH
    try:
        if curses.has_colors():
            curses.init_pair(40, curses.COLOR_BLACK, curses.COLOR_WHITE)
            _FLASH = curses.color_pair(40) | curses.A_BOLD
        else:
            _FLASH = curses.A_REVERSE | curses.A_BOLD
    except Exception:
        _FLASH = 201
    return _FLASH


class State:
    def __init__(self) -> None:
        style, snooze = load_settings()
        self.alarm = Alarm(style, snooze)
        self.focus = 0
        self.player = Player()
        self._clips: dict[str, bytes] = {}

    def now(self) -> float:
        import time
        return time.time()

    def advance(self) -> None:
        action = self.alarm.advance(self.now())
        if action == "play":
            style = self.alarm.style
            payload = self._clips.get(style)
            if payload is None:
                payload = wav_bytes(style)
                self._clips[style] = payload
            self.player.play(payload)

    def fields(self) -> tuple[str, ...]:
        if self.alarm.style == "clock":
            return ("time", "style", "snooze")
        return ("time", "style")


def render(surface, state: State) -> None:
    alarm = state.alarm
    if alarm.phase == "ringing" and alarm.style == "flash" and alarm.flash_lit:
        height, width = surface.getmaxyx()
        attr = flash_attr()
        blank = " " * max(0, width)
        for row in range(height):
            shell.put(surface, row, 0, blank, attr)
        shell.put(surface, 0, 1, "Alarm", attr)
        return
    body = shell.draw(
        surface,
        title="Alarm",
        sections=("Alarm",),
        summary=alarm.summary(state.now()),
        footer=_footer(alarm),
    )
    if body.height <= 0 or body.width <= 0:
        return
    row = body.top
    mark = "▶" if state.focus == 0 else " "
    shown = alarm.when_text or ("7:30pm" if alarm.phase == "set" else alarm.message)
    if state.focus == 0 and alarm.phase != "ringing":
        shown += "_"
    _line(surface, row, body, f"{mark} Time    {shown}")
    row += 2
    for style in STYLES:
        if row >= body.bottom:
            return
        cursor = "▶" if style == alarm.style else " "
        _line(surface, row, body, f" {cursor} {LABELS[style]} — {BLURBS[style]}")
        row += 1
    row += 1
    if alarm.style == "clock" and row < body.bottom:
        mark = "▶" if state.focus == 2 else " "
        _line(surface, row, body, f"{mark} Snooze  {alarm.snooze_minutes} min")


def _line(surface, row: int, body, text: str) -> None:
    shell.put(surface, row, body.left, text[:body.width])


def _footer(alarm: Alarm) -> str:
    if alarm.phase == "ringing" and alarm.style == "clock":
        return "s snooze · space dismiss · q quit"
    if alarm.phase == "ringing":
        return "space dismiss · q quit"
    return "↑↓ field · ←→ change · Enter set · q quit"


def handle(key: int, state: State) -> bool:
    if keymap.is_quit(key):
        state.player.close()
        return False
    alarm = state.alarm
    if alarm.phase == "ringing":
        if key == ord("s"):
            if alarm.snooze(state.now()):
                state.player.close()
            return True
        if key in keymap.SELECT:
            alarm.dismiss()
            state.player.close()
        return True
    fields = state.fields()
    if key in keymap.UP:
        state.focus = (state.focus - 1) % len(fields)
    elif key in keymap.DOWN:
        state.focus = (state.focus + 1) % len(fields)
    elif fields[state.focus] == "style" and key in keymap.RIGHT:
        alarm.cycle_style(1)
        state.focus = min(state.focus, len(state.fields()) - 1)
    elif fields[state.focus] == "style" and key in keymap.LEFT:
        alarm.cycle_style(-1)
        state.focus = min(state.focus, len(state.fields()) - 1)
    elif fields[state.focus] == "snooze" and key in keymap.RIGHT:
        alarm.nudge_snooze(1)
    elif fields[state.focus] == "snooze" and key in keymap.LEFT:
        alarm.nudge_snooze(-1)
    elif fields[state.focus] == "time" and key in keymap.BACKSPACE:
        alarm.when_text = alarm.when_text[:-1]
    elif fields[state.focus] == "time" and 32 <= key < 127:
        character = chr(key).lower()
        if len(alarm.when_text) < 8 and (character.isdigit() or character in ":apm "):
            alarm.when_text += character
    elif key in keymap.ENTER:
        if not alarm.when_text.strip():
            alarm.message = "type a time first"
        else:
            try:
                alarm.arm_clock(alarm.when_text, state.now())
            except AlarmError as error:
                alarm.message = str(error)
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="kilix-alarm")
    parser.add_argument("--at", help="arm a clock time such as 7:30pm or 19:30")
    parser.add_argument("--in", dest="delay", type=int, help="arm this many seconds from now")
    parser.add_argument("--style", choices=STYLES)
    parser.add_argument("--screenshot")
    options = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if options.at and options.delay is not None:
        print("kilix-alarm: use either --at or --in", file=sys.stderr)
        return 2
    state = State()
    if options.style:
        state.alarm.style = options.style
    try:
        if options.at:
            state.alarm.arm_clock(options.at, state.now())
        elif options.delay is not None:
            state.alarm.arm_in(options.delay, state.now())
    except AlarmError as error:
        print(f"kilix-alarm: {error}", file=sys.stderr)
        return 2
    if options.screenshot:
        with open(options.screenshot, "w", encoding="utf-8") as output:
            output.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle, tick_ms=200, on_tick=lambda item: item.advance())


if __name__ == "__main__":
    raise SystemExit(main())
