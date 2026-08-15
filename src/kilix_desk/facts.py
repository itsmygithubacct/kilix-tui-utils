"""Read-only facts for the desktop's home screen.

The status rows are file reads only: the home screen renders on every
keystroke and must never block on a command. The one exception is the
security alert, asked of the OS password helper once per launch (and again
on `r`) through a bounded `sudo -n` — never per frame — because "the login
password is still the default" is exactly the kind of fact a home screen
exists to surface. Anything deeper (doctor, update status) belongs to the
`plebian-os` control TUI the System section launches.
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
