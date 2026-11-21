"""Non-interactive kilix-volume commands.

get, set, mute, unmute, and sinks read or change the default sink and return.
They do not open the mixer.
"""

from __future__ import annotations

import re
import sys

from .control import Sink, VolumeError, get, mute, set_percent, sinks, unmute

_INTEGER = re.compile(r"[+-]?[0-9]+")
COMMANDS = {"get", "set", "mute", "unmute", "sinks"}


def command_requested(argv: list[str]) -> bool:
    return bool(argv) and argv[0] in COMMANDS


def format_level(percent: int, muted: bool) -> str:
    if muted:
        return f"{percent}% muted"
    return f"{percent}%"


def format_sink_line(sink: Sink) -> str:
    mark = "*" if sink.default else " "
    description = sink.description or sink.name or sink.target
    name = sink.name or sink.target
    return f"{mark} {format_level(sink.percent, sink.muted)}  {description}  {name}"


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(
            "kilix-volume: expected get, set PERCENT, mute, unmute, or sinks",
            file=sys.stderr,
        )
        return 2
    command, rest = argv[0], argv[1:]
    if command == "set":
        return _set(rest)
    if rest:
        print(f"kilix-volume: {command} does not take arguments", file=sys.stderr)
        return 2
    try:
        if command == "get":
            sink = get()
            print(format_level(sink.percent, sink.muted))
        elif command == "mute":
            mute()
        elif command == "unmute":
            unmute()
        else:
            for sink in sinks():
                print(format_sink_line(sink))
    except VolumeError as error:
        print(f"kilix-volume: {error}", file=sys.stderr)
        return 1
    return 0


def _set(rest: list[str]) -> int:
    if len(rest) != 1 or _INTEGER.fullmatch(rest[0]) is None:
        print("kilix-volume: volume must be an integer", file=sys.stderr)
        return 2
    try:
        set_percent(int(rest[0]))
    except VolumeError as error:
        print(f"kilix-volume: {error}", file=sys.stderr)
        return 1
    return 0
