"""Read-only facts for the desktop's home screen.

The status rows are file reads only: the home screen renders on every
keystroke and must never block on a command. The exceptions are asked once
per launch (and again on `r`), never per frame, and each is bounded: the
security alert through `sudo -n` and the OS password helper, because "the
login password is still the default" is exactly the kind of fact a home
screen exists to surface, and the volume row through `pactl`, because mixer
state has no file to read. Battery and network stay plain `/sys` reads —
the shared telemetry ring carries neither, so there is nothing to prefer
over reading them here. Anything deeper (doctor, update status) belongs to
the `plebian-os` control TUI the System section launches, and the
interactive controls stay in Machine; these rows only answer.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from kilix_desk import sources
from kilix_tui import proc

BUILD_INFO = "/etc/plebian-os/build-info.env"
PASSWD_HELPER = "/usr/local/sbin/plebian-os-passwd"
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


def default_password() -> bool:
    """True only when the login password is CONFIRMED still the default.

    Plebian-OS ships the account with a default password, plus a tiny root
    helper and a narrow passwordless-sudo rule so an unprivileged desktop
    can ask about it. Any uncertainty — no helper, no sudo rule, timeout,
    error — is False: the nag must never show spuriously, and a machine
    without the helper never pays for a subprocess that cannot answer.
    """
    if not (shutil.which("sudo") and os.access(PASSWD_HELPER, os.X_OK)):
        return False
    try:
        result = subprocess.run(["sudo", "-n", PASSWD_HELPER, "check"],
                                stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def alerts() -> list[str]:
    """Conditions worth interrupting the home screen with, one line each.

    Each line names the fault and where the fix lives, because an alert the
    user cannot act on is just decoration.
    """
    out: list[str] = []
    if default_password():
        out.append("default password still set — "
                   "change it in System › Change password")
    return out


def battery() -> tuple[str, str] | None:
    """The battery row, or None on machines without one — a desktop tower
    must not pay a row for hardware it does not have."""
    cells = proc.batteries()
    if not cells:
        return None
    parts = []
    for _name, capacity, status in cells:
        piece = f"{capacity}%" if capacity >= 0 else "?"
        if status and status != "unknown":
            piece = f"{piece} {status}"
        parts.append(piece)
    return ("battery", " · ".join(parts))


def network() -> tuple[str, str]:
    """Which links carry traffic, from the kernel's own operstate."""
    links = proc.network_links()
    if not links:
        return ("network", "no interfaces")
    up = [name for name, state in links if state == "up"]
    if up:
        return ("network", ", ".join(up) + " up")
    return ("network", f"all {len(links)} interfaces down")


def volume() -> str | None:
    """The default sink's volume, asked of `pactl` once per launch.

    Bounded and presence-gated like the password helper; any uncertainty —
    no pactl, no sound server, nothing parsable — is None, so the row is
    absent rather than a guess. The parse matches `kilix-volume`'s, and the
    default sink is preferred so this row and that tool agree.
    """
    if not shutil.which("pactl"):
        return None

    def ask(*args: str) -> str | None:
        try:
            result = subprocess.run(["pactl", *args],
                                    stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout if result.returncode == 0 else None

    listing = ask("list", "sinks")
    if listing is None:
        return None
    default = (ask("get-default-sink") or "").strip()
    sinks: list[tuple[str, int | None, bool]] = []
    name, level, muted = "", None, False
    for line in listing.splitlines():
        text = line.strip()
        if text.startswith("Sink #"):
            if name or level is not None:
                sinks.append((name, level, muted))
            name, level, muted = "", None, False
        elif text.startswith("Name:"):
            name = text.split(":", 1)[1].strip()
        elif text.startswith("Mute:"):
            muted = "yes" in text
        elif text.startswith("Volume:") and "%" in text and level is None:
            for token in text.split():
                if token.endswith("%"):
                    try:
                        level = int(token.rstrip("%"))
                    except ValueError:
                        pass
                    break
    if name or level is not None:
        sinks.append((name, level, muted))
    if not sinks:
        return None
    _name, level, muted = next(
        (sink for sink in sinks if sink[0] == default), sinks[0])
    if level is None:
        return None
    return f"muted ({level}%)" if muted else f"{level}%"


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
    if cell := battery():
        rows.append(cell)
    rows.append(network())
    if level := volume():
        rows.append(("volume", level))
    return rows
