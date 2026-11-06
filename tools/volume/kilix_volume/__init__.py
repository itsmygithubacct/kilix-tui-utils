"""Volume library for Kilix.

Importing this package does not start curses and does not talk to a sound
server. Call get, set, mute, unmute, or sinks to read or change the default
sink through whichever of pactl or wpctl is installed.
"""

from pathlib import Path

from .control import Sink, VolumeError, get, mute, set_percent, sinks, unmute

set = set_percent


def _repository_version() -> str:
    """Read the one version shared by every kilix-tui-utils command."""

    try:
        return (
            Path(__file__).resolve().parents[3] / "VERSION"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        return "0+unknown"


__version__ = _repository_version()

__all__ = [
    "Sink",
    "VolumeError",
    "get",
    "mute",
    "set",
    "sinks",
    "unmute",
]
