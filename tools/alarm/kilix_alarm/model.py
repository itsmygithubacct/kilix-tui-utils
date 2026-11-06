"""When an alarm fires, and which of the four styles it uses.

flash is a white screen and no tone. hum is one low, long, quiet tone.
alert is a short loud beep. clock repeats until it is snoozed or dismissed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

STYLES = ("flash", "hum", "alert", "clock")
LABELS = {
    "flash": "Flashing white",
    "hum": "Quiet hum",
    "alert": "Loud alert",
    "clock": "Alarm clock",
}
BLURBS = {
    "flash": "no sound",
    "hum": "low and long",
    "alert": "loud beep",
    "clock": "snooze",
}
SNOOZE_MIN = 1
SNOOZE_MAX = 30
SNOOZE_DEFAULT = 9
MAX_DELAY = 7 * 24 * 3600
_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?(am|pm)?")


class AlarmError(ValueError):
    """A time or style the alarm cannot use."""


@dataclass(frozen=True)
class Tone:
    hz: float
    seconds: float
    amplitude: float
    wave: str


def pattern(style: str) -> tuple[Tone, ...]:
    """One cycle of `style`. Flash is silent."""
    if style == "hum":
        return (Tone(110, 1.8, 0.06, "sine"), Tone(0, 1.2, 0, "silence"))
    if style == "alert":
        beeps: list[Tone] = []
        for _ in range(4):
            beeps.append(Tone(880, 0.09, 0.55, "square"))
            beeps.append(Tone(0, 0.07, 0, "silence"))
        beeps.append(Tone(0, 0.5, 0, "silence"))
        return tuple(beeps)
    if style == "clock":
        rings: list[Tone] = []
        for _ in range(3):
            rings.append(Tone(1046, 0.16, 0.35, "square"))
            rings.append(Tone(1318, 0.16, 0.35, "square"))
            rings.append(Tone(0, 0.08, 0, "silence"))
        rings.append(Tone(0, 0.7, 0, "silence"))
        return tuple(rings)
    if style == "flash":
        return ()
    raise AlarmError(f"unknown style {style}")


def cycle_seconds(style: str) -> float:
    return sum(tone.seconds for tone in pattern(style)) or 0.4


def parse_clock(text: str) -> tuple[int, int]:
    """Return 24-hour hour and minute. `7:30pm` and `19:30` both work."""
    compact = "".join(text.strip().lower().split())
    match = _CLOCK.fullmatch(compact)
    if match is None:
        raise AlarmError("use 7:30pm or 19:30")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = match.group(3)
    if minute > 59:
        raise AlarmError("use 7:30pm or 19:30")
    if ampm:
        if not 1 <= hour <= 12:
            raise AlarmError("use 7:30pm or 19:30")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif not 0 <= hour <= 23:
        raise AlarmError("use 7:30pm or 19:30")
    return hour, minute


def clock_text(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def next_fire(hour: int, minute: int, now: float) -> float:
    """The next local occurrence of hour:minute, today or tomorrow."""
    moment = datetime.fromtimestamp(now)
    target = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= moment:
        target += timedelta(days=1)
    return target.timestamp()


def delay_seconds(amount: int, unit: str) -> int:
    name = unit.lower()
    if name.startswith("h"):
        seconds = amount * 3600
    else:
        seconds = amount * 60
    if amount < 1 or seconds > MAX_DELAY:
        raise AlarmError("pick a time within the next 7 days")
    return seconds


def settings_path() -> Path:
    override = os.environ.get("KILIX_ALARM_SETTINGS")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("KILIX_STORAGE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local/gpu_terminal/kilix"
    return root / "alarm.json"


def load_settings() -> tuple[str, int]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "clock", SNOOZE_DEFAULT
    style = data.get("style")
    if style not in STYLES:
        style = "clock"
    try:
        snooze = int(data.get("snooze_minutes", SNOOZE_DEFAULT))
    except (TypeError, ValueError):
        snooze = SNOOZE_DEFAULT
    return style, min(SNOOZE_MAX, max(SNOOZE_MIN, snooze))


def save_settings(style: str, snooze_minutes: int) -> None:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.write_text(
            json.dumps({"style": style, "snooze_minutes": snooze_minutes}) + "\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
    except OSError:
        return


def remaining(seconds: float) -> str:
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole}s"
    minutes, sec = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {sec}s"


class Alarm:
    """One alarm held by the open window. Quitting drops it."""

    def __init__(self, style: str = "clock", snooze_minutes: int = SNOOZE_DEFAULT) -> None:
        if style not in STYLES:
            raise AlarmError(f"unknown style {style}")
        self.style = style
        self.snooze_minutes = min(SNOOZE_MAX, max(SNOOZE_MIN, snooze_minutes))
        self.when_text = ""
        self.phase = "set"
        self.fire_at: float | None = None
        self.snooze_until: float | None = None
        self.flash_lit = False
        self.message = ""
        self._next_sound = 0.0
        self._flash_mark = 0.0

    def set_style(self, style: str) -> None:
        if style not in STYLES:
            raise AlarmError(f"unknown style {style}")
        self.style = style
        save_settings(self.style, self.snooze_minutes)

    def cycle_style(self, step: int) -> None:
        index = STYLES.index(self.style)
        self.set_style(STYLES[(index + step) % len(STYLES)])

    def nudge_snooze(self, step: int) -> None:
        self.snooze_minutes = min(
            SNOOZE_MAX, max(SNOOZE_MIN, self.snooze_minutes + step)
        )
        save_settings(self.style, self.snooze_minutes)

    def arm_clock(self, text: str, now: float) -> None:
        hour, minute = parse_clock(text)
        self.when_text = text.strip()
        self.fire_at = next_fire(hour, minute, now)
        self.phase = "waiting"
        self.flash_lit = False
        self.snooze_until = None
        when = datetime.fromtimestamp(self.fire_at)
        day = "tomorrow " if when.date() != datetime.fromtimestamp(now).date() else ""
        self.message = f"set for {day}{clock_text(hour, minute)}"

    def arm_in(self, seconds: int, now: float) -> None:
        if seconds < 1 or seconds > MAX_DELAY:
            raise AlarmError("pick a time within the next 7 days")
        self.fire_at = now + seconds
        self.phase = "waiting"
        self.flash_lit = False
        self.snooze_until = None
        self.message = f"set for {remaining(seconds)}"

    def snooze(self, now: float) -> bool:
        if self.phase != "ringing" or self.style != "clock":
            self.message = "snooze is part of the alarm clock"
            return False
        self.phase = "snoozed"
        self.snooze_until = now + self.snooze_minutes * 60
        self.flash_lit = False
        self.message = f"snoozed {self.snooze_minutes}m"
        return True

    def dismiss(self) -> None:
        self.phase = "set"
        self.fire_at = None
        self.snooze_until = None
        self.flash_lit = False
        self.message = "dismissed"

    def advance(self, now: float) -> str | None:
        """Move the clock. Return `play`, `flash`, or None."""
        if self.phase == "waiting" and self.fire_at is not None and now >= self.fire_at:
            self._start_ring(now)
        elif (self.phase == "snoozed" and self.snooze_until is not None
              and now >= self.snooze_until):
            self._start_ring(now)
        if self.phase != "ringing":
            self.flash_lit = False
            return None
        if self.style == "flash":
            if now - self._flash_mark >= 0.4:
                self.flash_lit = not self.flash_lit
                self._flash_mark = now
            return "flash"
        if now >= self._next_sound:
            self._next_sound = now + cycle_seconds(self.style)
            return "play"
        return None

    def _start_ring(self, now: float) -> None:
        self.phase = "ringing"
        self.message = f"ringing — {LABELS[self.style]}"
        self._next_sound = now
        self._flash_mark = now
        self.flash_lit = self.style == "flash"

    def summary(self, now: float) -> str:
        if self.phase == "ringing":
            return self.message
        if self.phase in {"waiting", "snoozed"} and self.fire_at is not None:
            due = self.snooze_until if self.phase == "snoozed" and self.snooze_until else self.fire_at
            return f"{self.message} · {remaining(due - now)}"
        return "Enter sets it. This window stays open."
