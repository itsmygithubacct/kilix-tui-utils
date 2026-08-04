"""Read-only facts for the desktop's home screen.

File reads only, no subprocesses: the home screen renders on every keystroke
and must never block on a command. Anything deeper (doctor, update status)
belongs to the `plebian-os` control TUI the System section launches.
"""
from __future__ import annotations

import os

from kilix_desk import sources
from kilix_tui import proc

BUILD_INFO = "/etc/plebian-os/build-info.env"
COMPONENTS = (
    ("plebian-os", "plebian-os"),
    ("pleb", "pleb"),
    ("kilix", "kilix"),
    ("kilix-95", os.path.join("kilix-desktops", "kilix-95")),
    ("kilix-tui-utils",
     os.path.join("kilix-desktops", "kilix-tui-utils")),
)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def build_info() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read(BUILD_INFO).splitlines():
        key, _, value = line.strip().partition("=")
        if key and not key.startswith("#"):
            values[key] = value.strip("'\"")
    return values


def source_home() -> str:
    return sources.source_home()


def status_rows() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if release := build_info().get("PLEBIAN_OS_VERSION"):
        rows.append(("release", release))
    for name, relative_path in COMPONENTS:
        version = _read(
            os.path.join(sources.component_dir(relative_path),
                         "VERSION")).strip()
        rows.append((name, version or "not present"))
    rows.append(("provider", os.environ.get("KILIX_DESKTOP_PROVIDER", "auto")))
    rows.append(("uptime", proc.human_duration(proc.uptime_seconds())))
    return rows
