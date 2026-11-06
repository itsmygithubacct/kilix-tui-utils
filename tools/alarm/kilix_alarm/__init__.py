"""Alarm times, styles, and the tone each style plays."""

from .model import (
    STYLES,
    Alarm,
    AlarmError,
    clock_text,
    parse_clock,
)
from .sound import wav_bytes

__all__ = [
    "STYLES",
    "Alarm",
    "AlarmError",
    "clock_text",
    "parse_clock",
    "wav_bytes",
]
