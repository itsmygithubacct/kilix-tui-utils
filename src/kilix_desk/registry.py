"""What the desktop can launch, and how each thing is found.

Resolution follows the discipline Kilix 95's Start menu already enforces for
the model store: the installed command wins, a `kilix` subcommand that
installs-then-runs is the fallback, and a bare source checkout never shadows
either — except for this repository's own tools, where the desktop and the
tool are the same checkout by construction.

Two verbs. `inplace` hands the terminal to the tool and takes it back on
exit — the floor, available everywhere text works. `tab` opens a Kilix page
when remote control is reachable and quietly degrades to `inplace` when it is
not, so the same registry serves a Kilix pane, an `ssh` session, and a bare
console.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class Item:
    label: str
    command: str | None = None       # installed name on PATH — always wins
    sibling: str | None = None       # tools/<dir> in this checkout
    kilix: tuple[str, ...] = ()      # `kilix <subcommand>` fallback
    verb: str = "inplace"
    kilix_only: bool = False         # hidden outside a Kilix session


@dataclass(frozen=True)
class Plan:
    argv: tuple[str, ...]
    verb: str


PROGRAMS = (
    Item("Coding agents", command="kilix-rollout-resume",
         sibling="rollout_resume"),
    Item("Model store", command="kilix-bonsai", kilix=("bonsai",), verb="tab"),
    Item("Games", kilix=("games",), verb="tab", kilix_only=True),
    Item("Music", command="kilix-music", sibling="music"),
    Item("Weather", command="kilix-weather", sibling="weather"),
    Item("Calculator", command="kilix-calculator", sibling="calculator"),
    Item("Read aloud", command="kilix-tts"),
    Item("Dictation", command="kilix-stt"),
    Item("File manager", command="kilix-file", sibling="file"),
)

MACHINE = (
    Item("CPU", command="kilix-cpu", sibling="cpu"),
    Item("Memory", command="kilix-memory", sibling="memory"),
    Item("Temperatures", command="kilix-temps", kilix=("temps",), verb="tab"),
    Item("Disk", command="kilix-disk", sibling="disk"),
    Item("System facts", command="kilix-system", sibling="system"),
    Item("Volume", command="kilix-volume", sibling="volume"),
    Item("Network", command="nmtui"),
    Item("Packages", command="kilix-package", sibling="package"),
)

SYSTEM = (
    Item("OS control", command="plebian-os", sibling="plebian_control"),
    Item("Chrome settings", command="kilix-settings", kilix=("settings",)),
    # `kilix desktop <name>` opens its own page and returns at once, so these
    # run in place: launching them in a page of ours would leave a dead tab
    # behind the one the launcher opens.
    Item("Kilix 95 desktop", kilix=("desktop", "95"), kilix_only=True),
    Item("Kilix Cap desktop", kilix=("desktop", "kilix-cap"), kilix_only=True),
)

SESSION = (
    Item("Session logs", command="kilix-session-log", sibling="session_log"),
    Item("Switcher", command="kilix-switch", sibling="switcher",
         kilix_only=True),
    Item("PTY sessions", kilix=("pty",), kilix_only=True),
    Item("Tmux manager", command="tmux-tui"),
)

# Six sections, not more: the spine gives each section three rows, so six is
# what a standard 24-row terminal can label. The file manager lives under
# Programs for exactly this reason.
SECTIONS: dict[str, tuple[Item, ...]] = {
    "Programs": PROGRAMS,
    "Machine": MACHINE,
    "System": SYSTEM,
    "Session": SESSION,
}


def kilix_command() -> list[str] | None:
    """The `kilix` launcher, resolved the way `theme.py` finds the SDK."""
    source = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or os.path.join(
        os.path.expanduser("~"), "gpu_terminal")
    for base in (os.environ.get("KILIX_HOME", ""),
                 os.path.join(source, "kilix")):
        path = os.path.join(base, "kilix") if base else ""
        if path and os.access(path, os.X_OK):
            return [path]
    found = shutil.which("kilix")
    return [found] if found else None


def resolve(item: Item) -> Plan | None:
    """The argv for `item`, or None when nothing provides it."""
    if item.command:
        found = shutil.which(item.command)
        if found:
            return Plan((found,), item.verb)
    if item.sibling:
        path = os.path.join(ROOT, "tools", item.sibling, "main.py")
        if os.path.isfile(path):
            return Plan((sys.executable, path), item.verb)
    if item.kilix:
        launcher = kilix_command()
        if launcher:
            return Plan((*launcher, *item.kilix), item.verb)
    return None


def disabled_reason(item: Item) -> str:
    """One line saying why, and what fixes it — shown in place of the item."""
    if item.sibling:
        return "not installed — run kilix-tui-utils/install.sh"
    if item.kilix:
        return "needs a Kilix checkout"
    return "not installed"
