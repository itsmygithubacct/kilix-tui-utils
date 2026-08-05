"""The one place that names privileged and destructive commands.

Power belongs to the desktop and to the control TUI alike, and two lists of
`systemctl` invocations would eventually disagree about something as serious
as what "Shut down" actually runs. Both draw from here.

Nothing in this module executes anything. It returns argv lists and leaves
confirmation and execution to the caller, which is what lets the tests pin the
exact commands without a terminal or root.

FROZEN CONTRACT: the Kilix host's `kilix power logout|reboot|poweroff` verb
mirrors these exact argvs for the desktops that cannot import Python (Cap,
Land, 95's shutdown dialog). The two lists must never diverge — a change here
is a change there, in the same landing, and `tests/test_desktop.py` pins the
argvs on this side.
"""
from __future__ import annotations

import os


def power_actions() -> list[tuple[str, list[str], bool]]:
    """(label, argv, needs_confirmation) for the session/power controls.

    Every entry confirms: these are the actions that can strand a machine or
    end a session, and a desktop puts them one keystroke closer than a shell
    ever did.
    """
    return [
        ("Log out of this session",
         ["loginctl", "terminate-session",
          os.environ.get("XDG_SESSION_ID", "")],
         True),
        ("Reboot", ["systemctl", "reboot"], True),
        ("Shut down", ["systemctl", "poweroff"], True),
    ]
