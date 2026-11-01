"""The XDG application entry that puts the manager in Kilix 95's Start menu.

Kilix 95 builds Start > Programs from installed ``.desktop`` files; an entry
with ``Categories=System`` lands under Programs > System, and
``Terminal=true`` opens it in a Kilix tab. ``install.sh`` writes this entry;
it replaces only a file carrying this module's marker, never someone else's.
"""
from __future__ import annotations

import os
import re

MARKER = "X-Kilix-QEMU-Manager=true"
FILENAME = "kilix-qemu-manager.desktop"
_PLAIN = re.compile(r"[A-Za-z0-9/._+@-]+\Z")
_UNQUOTABLE = set('"`$\\%\n\r\t')


def default_applications_dir() -> str:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(data_home, "applications")


def exec_word(path: str) -> str | None:
    """One Exec argument per the Desktop Entry spec, or None if unsafe."""
    if _PLAIN.fullmatch(path):
        return path
    if any(char in _UNQUOTABLE for char in path):
        return None
    return f'"{path}"'


def entry(command: str) -> str:
    word = exec_word(command)
    if word is None:
        raise ValueError(f"cannot quote {command!r} for a desktop entry")
    return (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        "Name=QEMU VMs\n"
        "GenericName=Virtual Machine Manager\n"
        "Comment=Pick, launch and control QEMU virtual machines in Kilix tabs\n"
        f"Exec={word}\n"
        f"TryExec={command}\n"
        "Icon=computer\n"
        "Terminal=true\n"
        "Categories=System;Emulator;\n"
        "Keywords=qemu;kvm;vm;virtual;machine;favorites;\n"
        "StartupNotify=false\n"
        "X-Kilix-Open=tab\n"
        f"{MARKER}\n"
    )


def _managed(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            return MARKER in handle.read()
    except (OSError, UnicodeDecodeError):
        return False


def install(command: str, applications_dir: str = "") -> tuple[str, bool]:
    """Write the entry; return (path, changed). Never replace a foreign file."""
    directory = applications_dir or default_applications_dir()
    path = os.path.join(directory, FILENAME)
    text = entry(command)
    if os.path.lexists(path):
        if not _managed(path) or os.path.islink(path):
            raise FileExistsError(f"{path} exists and is not managed here")
        with open(path, encoding="utf-8") as handle:
            if handle.read() == text:
                return path, False
    os.makedirs(directory, exist_ok=True)
    swap = f"{path}.tmp.{os.getpid()}"
    with open(swap, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(swap, 0o644)
    os.replace(swap, path)
    return path, True
