"""Read and write sinks through pactl or wpctl.

Nothing in this module runs at import. pactl is preferred when both control
commands are installed. Writes use an integer percent clamped to 0–150, the
same range as the mixer. Mute and unmute set the flag; they do not toggle.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

CONTROLS = ("pactl", "wpctl")
MIN_PERCENT = 0
MAX_PERCENT = 150

MISSING_CONTROL = (
    "No PulseAudio or PipeWire control command found. "
    "Install pulseaudio-utils (pactl) or wireplumber (wpctl)."
)

_NODE = re.compile(
    r"^(?P<star>\*)?\s*(?P<id>\d+)\.\s+(?P<label>.*?)(?:\s+\[(?P<bracket>[^\]]*)\])?\s*$"
)
_LINEAR = re.compile(r"vol:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)
_TREE = str.maketrans({
    "│": " ", "├": " ", "└": " ", "─": " ",
    "┌": " ", "┐": " ", "┤": " ", "┘": " ",
})
_SECTIONS = {"Audio", "Video", "Settings"}


class VolumeError(Exception):
    """A volume read or write could not be completed."""


@dataclass(frozen=True)
class Sink:
    """One output sink.

    `percent` is an integer percent. `target` is the id the control command
    accepts: a PulseAudio name, or a PipeWire object id.
    """

    name: str
    description: str
    percent: int
    muted: bool
    default: bool = False
    target: str = ""


def which_control() -> str | None:
    """Return `pactl` or `wpctl`, preferring pactl when both exist."""

    for name in CONTROLS:
        if shutil.which(name):
            return name
    return None


def clamp(percent: int) -> int:
    """Clamp an integer percent to the mixer range, 0 through 150."""

    if isinstance(percent, bool) or not isinstance(percent, int):
        raise VolumeError("volume must be an integer")
    if percent < MIN_PERCENT:
        return MIN_PERCENT
    if percent > MAX_PERCENT:
        return MAX_PERCENT
    return percent


def get() -> Sink:
    """Return the default sink."""

    found = sinks()
    for sink in found:
        if sink.default:
            return sink
    raise VolumeError("No default audio sink is available.")


def set_percent(percent: int) -> None:
    """Set the default sink to `percent`, clamped to 0–150."""

    command = _require_control()
    level = clamp(percent)
    if command == "pactl":
        _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
        return
    # wpctl treats a trailing % as an absolute percent. A bare float is linear
    # (1.0 == 100%), and a trailing + or - steps instead of setting.
    _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level}%"])


def set_target(target: str, percent: int) -> None:
    """Set one sink by the id the active control command accepts."""

    command = _require_control()
    if not target:
        raise VolumeError("No audio sink was named.")
    level = clamp(percent)
    if command == "pactl":
        _run(["pactl", "set-sink-volume", target, f"{level}%"])
        return
    _run(["wpctl", "set-volume", target, f"{level}%"])


def mute() -> None:
    """Mute the default sink. This sets the flag; it does not toggle."""

    _mute_default(True)


def unmute() -> None:
    """Unmute the default sink. This sets the flag; it does not toggle."""

    _mute_default(False)


def toggle_mute(target: str) -> None:
    """Toggle one sink. The mixer key does this; mute and unmute do not."""

    _mute_target(target, None)


def set_default(target: str) -> None:
    """Make one sink the default."""

    command = _require_control()
    if not target:
        raise VolumeError("No audio sink was named.")
    if command == "pactl":
        _run(["pactl", "set-default-sink", target])
        return
    _run(["wpctl", "set-default", target])


def sinks() -> list[Sink]:
    """Return every sink, with the default marked."""

    return _list(_require_control())


def parse_pactl_sinks(listing: str, default_name: str) -> list[Sink]:
    """Parse `pactl list sinks` and mark `default_name`."""

    default_name = default_name.strip()
    rows: list[dict[str, object]] = []
    current: dict[str, object] | None = None

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        rows.append(current)
        current = None

    for line in listing.splitlines():
        text = line.strip()
        if text.startswith("Sink #"):
            finish()
            current = {
                "index": text.split("#", 1)[1].strip(),
                "name": "",
                "description": "",
                "percent": 0,
                "muted": False,
            }
            continue
        if current is None:
            continue
        if text.startswith("Name:"):
            current["name"] = text.split(":", 1)[1].strip()
        elif text.startswith("Description:"):
            current["description"] = text.split(":", 1)[1].strip()
        elif text.startswith("Mute:"):
            current["muted"] = "yes" in text.lower()
        elif text.startswith("Volume:") and "%" in text:
            for token in text.split():
                if token.endswith("%"):
                    try:
                        current["percent"] = int(token.rstrip("%"))
                    except ValueError:
                        pass
                    break
    finish()

    parsed: list[Sink] = []
    for row in rows:
        name = str(row["name"])
        index = str(row["index"])
        percent = row["percent"]
        parsed.append(Sink(
            name=name,
            description=str(row["description"]),
            percent=percent if isinstance(percent, int) else 0,
            muted=bool(row["muted"]),
            default=bool(default_name) and name == default_name,
            target=name or index,
        ))
    return parsed


def parse_wpctl_status(status: str, named: str = "") -> list[Sink]:
    """Parse `wpctl status`, optionally joining names from `wpctl status --name`."""

    described = _wpctl_nodes(status)
    names = {node.id: node.label for node in _wpctl_nodes(named)} if named else {}
    parsed: list[Sink] = []
    for node in described:
        name = names.get(node.id) or node.label
        parsed.append(Sink(
            name=name,
            description=node.label,
            percent=node.percent,
            muted=node.muted,
            default=node.default,
            target=node.id,
        ))
    return parsed


def _mute_default(muted: bool) -> None:
    command = _require_control()
    flag = "1" if muted else "0"
    if command == "pactl":
        _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", flag])
        return
    _run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", flag])


def _mute_target(target: str, muted: bool | None) -> None:
    command = _require_control()
    if not target:
        raise VolumeError("No audio sink was named.")
    flag = "toggle" if muted is None else ("1" if muted else "0")
    if command == "pactl":
        _run(["pactl", "set-sink-mute", target, flag])
        return
    _run(["wpctl", "set-mute", target, flag])


def _require_control() -> str:
    command = which_control()
    if command is None:
        raise VolumeError(MISSING_CONTROL)
    return command


def _list(command: str) -> list[Sink]:
    if command == "pactl":
        default = _run(["pactl", "get-default-sink"]).stdout
        listing = _run(["pactl", "list", "sinks"]).stdout
        return parse_pactl_sinks(listing, default)
    status = _run(["wpctl", "status"]).stdout
    try:
        named = _run(["wpctl", "status", "--name"]).stdout
    except VolumeError:
        named = ""
    return parse_wpctl_status(status, named)


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    # Parse English pactl keys and dot decimals from wpctl.
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise VolumeError(f"{argv[0]} failed: {error}") from error
    if completed.returncode != 0:
        text = (completed.stderr or completed.stdout or "").strip()
        detail = text.splitlines()[0] if text else "command failed"
        raise VolumeError(f"{argv[0]} exited {completed.returncode}: {detail}")
    return completed


@dataclass(frozen=True)
class _Node:
    id: str
    label: str
    percent: int
    muted: bool
    default: bool


def _bare(line: str) -> str:
    text = line.translate(_TREE).strip()
    while text[:1] in {"|", "`"}:
        text = text[1:].lstrip("-").strip()
    return text


def _percent_from_linear(token: str) -> int:
    try:
        value = (Decimal(token) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except Exception:
        return 0
    return int(value)


def _wpctl_nodes(status: str) -> list[_Node]:
    section = ""
    subsection = ""
    found: list[_Node] = []
    for line in status.splitlines():
        bare = _bare(line)
        if not bare:
            continue
        if bare in _SECTIONS:
            section = bare
            subsection = ""
            continue
        if bare.endswith(":") and _NODE.match(bare) is None:
            subsection = bare[:-1].strip().lower()
            continue
        if section != "Audio" or subsection != "sinks":
            continue
        match = _NODE.match(bare)
        if match is None:
            continue
        bracket = match.group("bracket") or ""
        volume = _LINEAR.search(bracket)
        found.append(_Node(
            id=match.group("id"),
            label=match.group("label").strip(),
            percent=_percent_from_linear(volume.group(1)) if volume else 0,
            muted="MUTED" in bracket.upper(),
            default=match.group("star") is not None,
        ))
    return found
