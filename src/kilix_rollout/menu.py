"""Kilix-95 start-menu entries.

Kilix-95 builds its Start menu from XDG application entries, so shipping a
`.desktop` file is all it takes to appear under Programs. `Terminal=true` makes
it open in a Kilix tab.

The picker entry is always installed. An update entry is written per agent only
while that agent is actually installed, and removed when it isn't — a menu item
that updates software you don't have is a dead end, so the menu is re-synced
rather than written once.
"""
from __future__ import annotations

import os

MARKER = "X-Kilix-Rollout-Resume=true"
PICKER = "kilix-rollout-resume.desktop"

_TEMPLATE = """[Desktop Entry]
Version=1.0
Type=Application
Name={name}
GenericName={generic}
Comment={comment}
Exec={exec}
TryExec=kilix-rollout-resume
Icon={icon}
Terminal=true
Categories=Development;
Keywords={keywords}
StartupNotify=false
X-Kilix-Open=tab
{marker}
"""


def default_applications_dir() -> str:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(data_home, "applications")


def picker_entry() -> str:
    return _TEMPLATE.format(
        name="Kilix Rollout Resume",
        generic="Coding Session Recovery",
        comment="Resume Claude Code, Codex, and Kimi Code sessions",
        exec="kilix-rollout-resume",
        icon="utilities-terminal",
        keywords="claude;codex;kimi;session;resume;recovery;",
        marker=MARKER,
    )


def update_entry(item) -> str:
    return _TEMPLATE.format(
        name=f"Update {item.label}",
        generic="Coding Agent Update",
        comment=f"Check for and install a newer {item.label}",
        exec=f"kilix-rollout-resume update {item.key}",
        icon="system-software-update",
        keywords=f"{item.key};update;upgrade;",
        marker=MARKER,
    )


def update_name(item) -> str:
    return f"kilix-update-{item.key}.desktop"


def _managed(path: str) -> bool:
    """True only for entries this tool wrote, so nothing else is touched."""
    try:
        with open(path, encoding="utf-8") as handle:
            return MARKER in handle.read()
    except OSError:
        return False


def _write(path: str, text: str) -> None:
    temporary = f"{path}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def sync(providers, *, applications_dir: str = "", is_installed=None) -> dict:
    """Write the picker entry and one update entry per installed agent.

    Returns the paths written and removed, so a caller can report what changed.
    """
    if is_installed is None:
        from . import manage
        is_installed = lambda item: bool(manage.installed(item))  # noqa: E731

    directory = applications_dir or default_applications_dir()
    os.makedirs(directory, exist_ok=True)

    written: list[str] = []
    removed: list[str] = []

    picker = os.path.join(directory, PICKER)
    _write(picker, picker_entry())
    written.append(picker)

    for item in providers:
        path = os.path.join(directory, update_name(item))
        if is_installed(item):
            _write(path, update_entry(item))
            written.append(path)
        elif os.path.exists(path) and _managed(path):
            os.remove(path)
            removed.append(path)
    return {"written": written, "removed": removed}


def remove(providers, *, applications_dir: str = "") -> list[str]:
    """Remove every entry this tool manages."""
    directory = applications_dir or default_applications_dir()
    gone: list[str] = []
    names = [PICKER] + [update_name(item) for item in providers]
    for name in names:
        path = os.path.join(directory, name)
        if os.path.exists(path) and _managed(path):
            os.remove(path)
            gone.append(path)
    return gone
