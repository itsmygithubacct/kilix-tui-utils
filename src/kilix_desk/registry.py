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

from kilix_desk import sources

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class Item:
    label: str
    command: str | None = None       # installed name on PATH — always wins
    sibling: str | None = None       # tools/<dir> in this checkout
    source: str | None = None        # <dir>/main.py in this checkout
    kilix: tuple[str, ...] = ()      # `kilix <subcommand>` fallback
    verb: str = "inplace"            # inplace | tab | report
    kilix_only: bool = False         # hidden outside a Kilix session
    submenu: str = ""                # opens a drill-down list instead
    confirm: bool = False            # asks before running


@dataclass(frozen=True)
class Plan:
    argv: tuple[str, ...]
    verb: str


PROGRAMS = (
    Item("Coding agents", command="kilix-rollout-resume",
         sibling="rollout_resume"),
    Item("Model store", command="kilix-bonsai", kilix=("bonsai",), verb="tab"),
    Item("Web browser", kilix=("open-url",), verb="tab", kilix_only=True),
    Item("Games", submenu="games"),
    Item("Screensavers", submenu="screensavers", kilix_only=True),
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
    Item("Temperatures", command="kilix-temps", sibling="temps",
         kilix=("temps",), verb="tab"),
    Item("VirtualBox VPN", command="kilix-virtualbox-manager",
         source="kilix-virtualbox-manager"),
    Item("Disk", command="kilix-disk", sibling="disk"),
    Item("System facts", command="kilix-system", sibling="system"),
    Item("Volume", command="kilix-volume", sibling="volume"),
    Item("Network", command="nmtui"),
    Item("Packages", command="kilix-package", sibling="package"),
)

SYSTEM = (
    Item("OS control", command="plebian-os", sibling="plebian_control"),
    Item("Chrome settings", command="kilix-settings", kilix=("settings",)),
    Item("Screen size", kilix=("screen-size", "show"), verb="report"),
    Item("Stack status", kilix=("status",), verb="report"),
    Item("Voice status", kilix=("voice", "status"), verb="report"),
    Item("Voice doctor", kilix=("voice", "doctor"), verb="report"),
    Item("Update the stack", kilix=("update",), confirm=True),
    Item("Screen sharing", kilix=("share",), verb="tab", kilix_only=True),
    # `kilix desktop <name>` opens its own page and returns at once, so these
    # run in place: launching them in a page of ours would leave a dead tab
    # behind the one the launcher opens.
    Item("Kilix 95 desktop", kilix=("desktop", "95"), kilix_only=True),
    Item("Kilix XP desktop", kilix=("desktop", "kilix-xp"), kilix_only=True),
    Item("Kilix Cap desktop", kilix=("desktop", "kilix-cap"), kilix_only=True),
    Item("Kilix Land desktop", kilix=("desktop", "kilix-land"),
         kilix_only=True),
)

SESSION = (
    Item("New terminal", kilix=("new-tab",), kilix_only=True),
    Item("New pane", kilix=("new-pane",), kilix_only=True),
    Item("Switcher", command="kilix-switch", sibling="switcher",
         kilix_only=True),
    Item("Session logs", command="kilix-session-log", sibling="session_log"),
    Item("PTY sessions", kilix=("pty",), kilix_only=True),
    Item("Mux terminal", kilix=("mux",), kilix_only=True),
    Item("Tmux manager", command="tmux-tui"),
    # The streaming tiers: serve holds a session open, attach drives it,
    # view watches without a keyboard.
    Item("Serve this session", kilix=("serve",), kilix_only=True),
    Item("Attach to a session", kilix=("attach",), kilix_only=True),
    Item("Watch a session", kilix=("view",), kilix_only=True),
    Item("Compress dead transcripts", kilix=("transcript", "archive"),
         verb="report"),
    Item("Apply transcript budgets", kilix=("transcript", "prune"),
         verb="report", confirm=True),
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


def _sdk_settings():
    """`kilix_sdk.settings`, or None — resolved the way `theme.py` does."""
    import importlib
    for home in (os.environ.get("KILIX_HOME", ""),
                 sources.component_dir("kilix")):
        config = os.path.join(home, "config") if home else ""
        if config and os.path.isdir(os.path.join(config, "kilix_sdk")):
            if config not in sys.path:
                sys.path.insert(0, config)
            try:
                return importlib.import_module("kilix_sdk.settings")
            except Exception:
                return None
    return None


def games() -> list[tuple[str, str, bool]] | None:
    """(id, label, enabled) for every stack game, or None without Kilix.

    This is `kilix games list`, absorbed: the shared availability toggles the
    text menu prints are a list the desktop can show and flip directly.
    """
    sdk = _sdk_settings()
    if sdk is None:
        return None
    try:
        availability = sdk.game_availability()
        return [(game_id, label, bool(availability.get(game_id, True)))
                for game_id, label in sdk.GAME_TOGGLE_IDS]
    except Exception:
        return None


def screensavers() -> list[str]:
    """The screensaver names a Kilix checkout ships, `kilix screensaver X`."""
    home = os.environ.get("KILIX_HOME") or sources.component_dir("kilix")
    directory = os.path.join(home, "config", "screensavers")
    try:
        names = sorted(name[:-2] for name in os.listdir(directory)
                       if name.endswith(".c"))
    except OSError:
        return []
    return names


def kilix_command() -> list[str] | None:
    """The `kilix` launcher, resolved the way `theme.py` finds the SDK."""
    for base in (os.environ.get("KILIX_HOME", ""),
                 sources.component_dir("kilix")):
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
    if item.source:
        path = os.path.realpath(os.path.join(ROOT, item.source, "main.py"))
        root = os.path.realpath(ROOT) + os.sep
        if path.startswith(root) and os.path.isfile(path):
            return Plan((sys.executable, path), item.verb)
    if item.kilix:
        launcher = kilix_command()
        if launcher:
            return Plan((*launcher, *item.kilix), item.verb)
    return None


def disabled_reason(item: Item) -> str:
    """One line saying why, and what fixes it — shown in place of the item."""
    if item.sibling or item.source:
        return "not installed — run kilix-tui-utils/install.sh"
    if item.kilix:
        return "needs a Kilix checkout"
    return "not installed"
