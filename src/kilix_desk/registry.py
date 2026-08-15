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

from kilix_desk import manual, sources

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The Plebian-OS dependency reinstaller, deployed by the OS itself. Helper
# entries are presence-gated like the reference desktop's System menu: a
# machine without the helper hides the row rather than offering a command
# that can only fail.
DEPS_HELPER = "/usr/local/sbin/plebian-os-install-deps"


@dataclass(frozen=True)
class Item:
    label: str
    command: str | None = None       # installed name on PATH — always wins
    sibling: str | None = None       # tools/<dir> in this checkout
    source: str | None = None        # <dir>/main.py in this checkout
    kilix: tuple[str, ...] = ()      # `kilix <subcommand>` fallback
    topic: str = ""                  # a help-book topic, paged by manual.py
    verb: str = "inplace"            # inplace | tab | report
    kilix_only: bool = False         # hidden outside a Kilix session
    submenu: str = ""                # opens a drill-down list instead
    confirm: bool = False            # asks before running
    helper: str = ""                 # a root helper run via sudo; hidden when absent
    restart: bool = False            # a clean run re-execs the desktop


@dataclass(frozen=True)
class Plan:
    argv: tuple[str, ...]
    verb: str


PROGRAMS = (
    Item("Coding agents", command="kilix-rollout-resume",
         sibling="rollout_resume"),
    Item("Model store", command="kilix-bonsai", kilix=("bonsai",), verb="tab"),
    Item("Region painter", kilix=("mask",), verb="tab"),
    Item("PDF Conversion",
         kilix=("app", "run", "kilix-pdf-conversion"), verb="tab"),
    Item("Web browser", kilix=("open-url",), verb="tab", kilix_only=True),
    # Listed separately from "Web browser" because it is a different promise:
    # that one reaches for a desktop browser first, this one is always the
    # text browser in a pane. On a machine with no X server they end up at
    # the same program, and naming it is how you can ask for it directly.
    Item("Text browser", kilix=("chawan",), verb="tab", kilix_only=True),
    # The whole installable surface — catalog games and applications plus the
    # coding agents — behind the same command the CLI uses, so the desktop is
    # not a second list that can disagree with `kilix install`.
    Item("Kilix applications", submenu="catalog apps"),
    Item("Install software", submenu="software"),
    Item("Games", submenu="games"),
    Item("Screensavers", submenu="screensavers", kilix_only=True),
    # Everything the machine itself advertises: freedesktop `.desktop` entries
    # discovered the same way Kilix 95's Start menu discovers them, bucketed
    # by category. The stack programs above stay a curated list; this place is
    # the uncurated rest of the computer.
    Item("Applications", submenu="applications"),
    Item("Music", command="kilix-music", sibling="music"),
    Item("Weather", command="kilix-weather", sibling="weather"),
    Item("Calculator", command="kilix-calculator", sibling="calculator"),
    Item("Voice Studio", submenu="voice"),
    Item("Read aloud", command="kilix-tts"),
    Item("Dictation", command="kilix-stt"),
    Item("File manager", command="kilix-file", sibling="file"),
    Item("Find files", command="kilix-find-files", sibling="find_files"),
    Item("Notepad", command="kilix-notepad", sibling="notepad"),
    Item("Character map", command="kilix-character-map",
         sibling="character_map"),
)

VOICE = (
    Item("Read aloud", command="kilix-tts"),
    Item("Dictation", command="kilix-stt"),
    Item("Model store", command="kilix-bonsai", kilix=("bonsai",), verb="tab"),
    Item("Voice settings", command="kilix-settings", kilix=("settings",)),
    Item("Voice status", kilix=("voice", "status"), verb="report"),
    Item("Voice doctor", kilix=("voice", "doctor"), verb="report"),
)

MACHINE = (
    Item("CPU", command="kilix-cpu", sibling="cpu"),
    Item("Memory", command="kilix-memory", sibling="memory"),
    Item("Temperatures", command="kilix-temps", sibling="temps",
         kilix=("temps",), verb="tab"),
    Item("VirtualBox VPN", command="kilix-virtualbox-manager",
         source="kilix-virtualbox-manager"),
    Item("Cameras", command="kilix-cameras", sibling="cameras"),
    Item("Disk", command="kilix-disk", sibling="disk"),
    Item("System facts", command="kilix-system", sibling="system"),
    Item("Volume", command="kilix-volume", sibling="volume"),
    Item("Network", command="nmtui"),
    Item("Packages", command="kilix-package", sibling="package"),
)

SYSTEM = (
    Item("OS control", command="plebian-os", sibling="plebian_control"),
    # The stack's help book — the topics Kilix 95's Help renders, with the
    # recovery guide one Enter away rather than buried inside OS control.
    Item("Manual", submenu="manual"),
    # `passwd` behind the held-output wrapper: the change itself is
    # interactive, the result is worth reading, and Home's security alert
    # points here when the login password is still the shipped default.
    Item("Change password", command="passwd", verb="report"),
    Item("Audio settings", command="kilix-volume", sibling="volume"),
    Item("Chrome settings", command="kilix-settings", kilix=("settings",)),
    Item("Screen size", kilix=("screen-size", "show"), verb="report"),
    Item("Stack status", kilix=("status",), verb="report"),
    Item("Voice status", kilix=("voice", "status"), verb="report"),
    Item("Voice doctor", kilix=("voice", "doctor"), verb="report"),
    Item("Update the stack", kilix=("update",), confirm=True),
    # The same update, then a fresh desktop process on top of it — the only
    # way the menu you come back to is drawn by the code the update just
    # installed. Power stays the frozen three privileged argvs; this row is
    # maintenance, so it lives here.
    Item("Update and restart desktop", kilix=("update",), confirm=True,
         restart=True),
    Item("Reinstall dependencies", helper=DEPS_HELPER, verb="report",
         confirm=True),
    Item("Scripts", submenu="scripts"),
    Item("Screen sharing", kilix=("share",), verb="tab", kilix_only=True),
    # `kilix desktop <name>` opens its own page and returns at once, so these
    # run in place: launching them in a page of ours would leave a dead tab
    # behind the one the launcher opens.
    Item("Kilix 95 desktop", kilix=("desktop", "95"), kilix_only=True),
    Item("Kilix XP desktop", kilix=("desktop", "kilix-xp"), kilix_only=True),
    Item("Kilix Cap desktop", kilix=("desktop", "kilix-cap"), kilix_only=True),
    Item("Kilix Land desktop", kilix=("desktop", "kilix-land"),
         kilix_only=True),
    # Which desktop every later session starts with. The entries above run one
    # now; this is the one that persists the choice, and every desktop offers
    # it so the answer does not depend on which desktop you happen to be in.
    Item("Default desktop", submenu="default desktop"),
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
    """The one pinned SDK-settings loader shared with the TUI theme."""
    from kilix_tui import theme
    return theme.sdk_settings()


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


def applications() -> dict[str, list[dict]]:
    """Discovered freedesktop applications, {bucket: [entry, …]}.

    The scan itself is shared (`kilix_tui.xdgapps`) so a future catalog tool
    lists exactly what this desktop lists. Failure to scan is an empty
    catalog, not an error: a machine with no `.desktop` files is a state,
    not a fault.
    """
    from kilix_tui import xdgapps
    try:
        return xdgapps.grouped()
    except Exception:
        return {}


def games_play_supported(kilix: list[str]) -> bool:
    """Whether the host launcher knows `kilix games play <id>`.

    Probed from the usage line `kilix games help` prints, which lists every
    action the installed launcher accepts — the same text a user would read.
    Today's launchers answer list/settings/enable/disable; when the host
    grows `play`, the desktop's Enter starts launching games with no change
    here. The caller caches the answer per visit: this runs a subprocess.
    """
    import subprocess
    try:
        result = subprocess.run([*kilix, "games", "help"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return "play" in (result.stdout + result.stderr)


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


def helper_ready(path: str) -> bool:
    """Whether a root helper is installed where the OS deploys it.

    `sudo` must exist too: the helpers are root-only by design, and a row
    that cannot possibly run is noise, not an offer.
    """
    return bool(shutil.which("sudo")) and os.access(path, os.X_OK)


def script_dirs() -> list[str]:
    """The stack's scripts/ directories, gated on presence like the
    reference desktop's System menu."""
    dirs = [os.path.expanduser(os.path.join("~", "pleb", "scripts"))]
    kilix_home = os.environ.get("KILIX_HOME", "")
    if kilix_home:
        dirs.append(os.path.join(kilix_home, "scripts"))
    return dirs


def script_rows(dirs: list[str] | None = None) -> list[dict]:
    """Executable *.sh under the pleb/kilix scripts directories — the same
    files the reference desktop's System ▸ Scripts submenu offers. One
    source for the desk's Scripts place and the launcher catalog alike."""
    out: list[dict] = []
    seen: set[str] = set()
    for base in (script_dirs() if dirs is None else dirs):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if not name.endswith(".sh") or name in seen:
                continue
            path = os.path.join(base, name)
            if not os.path.isfile(path) or not os.access(path, os.X_OK):
                continue
            seen.add(name)
            out.append({"kind": "script", "label": name, "detail": "script",
                        "argv": [path], "verb": "inplace"})
    return out


def installable() -> list[dict] | None:
    """Everything `kilix install` offers, as it reports it.

    The desktop asks the launcher rather than reading the catalog itself. There
    is one list in this system and one thing that knows how to install from it;
    a second reader here would be a second catalogue to keep true.
    """
    import json
    import subprocess
    launcher = kilix_command()
    if launcher is None:
        return None
    try:
        result = subprocess.run([*launcher, "install", "--json"],
                                capture_output=True, text=True, timeout=30,
                                check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        rows = json.loads(result.stdout)
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def default_desktop() -> str | None:
    """The desktop every later session starts with, as Kilix reports it."""
    import subprocess
    launcher = kilix_command()
    if launcher is None:
        return None
    try:
        result = subprocess.run([*launcher, "default-desktop", "show"],
                                capture_output=True, text=True, timeout=15,
                                check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip().splitlines()
    return value[-1].strip() if result.returncode == 0 and value else None


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
    if item.topic:
        # The help book ships with the desktop, so this never misses: the
        # book and the desk are the same checkout by construction.
        return Plan((sys.executable, manual.PATH, item.topic), item.verb)
    if item.kilix:
        launcher = kilix_command()
        if launcher:
            return Plan((*launcher, *item.kilix), item.verb)
    if item.helper and helper_ready(item.helper):
        return Plan(("sudo", item.helper), item.verb)
    return None


def disabled_reason(item: Item) -> str:
    """One line saying why, and what fixes it — shown in place of the item."""
    if item.sibling or item.source:
        return "not installed — run kilix-tui-utils/install.sh"
    if item.kilix:
        return "needs a Kilix checkout"
    return "not installed"
