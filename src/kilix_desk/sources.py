"""Where the stack's source checkouts actually are.

`kilix` exports `GPU_TERMINAL_SOURCE_HOME` to everything it launches and
defaults it to `~/.local/gpu_terminal/sources`, which is correct on an
installed Plebian-OS but is frequently absent on a development machine, where
the checkouts sit in `~/gpu_terminal`. Trusting the variable without checking
it made every lookup that depends on it fail *silently*: the home screen
reported every component "not present", and the Games and Screensavers menus
came back empty, because a missing directory and an unreadable one are the
same `OSError` to the caller.

So resolution is a search over candidates that must exist, not a single
variable read. The last candidate is this checkout's own workspace, which is
the one root that is true by construction whenever the desktop is running from
source at all.

Component paths are looked up under both the umbrella layout
(`kilix-desktops/kilix-95`) and the flat one (`kilix-95`); Kilix's own launcher
accepts both, so this must too.
"""
from __future__ import annotations

import os

# .../<workspace>/kilix-desktops/kilix-tui-utils/src/kilix_desk/sources.py
_CHECKOUT = os.path.dirname(  # kilix-tui-utils
    os.path.dirname(          # src
        os.path.dirname(os.path.abspath(__file__))))
_WORKSPACE = os.path.dirname(os.path.dirname(_CHECKOUT))  # above kilix-desktops


def candidates() -> tuple[str, ...]:
    """Every plausible workspace root, best first."""
    return tuple(
        path for path in (
            os.environ.get("GPU_TERMINAL_SOURCE_HOME", ""),
            os.path.join(os.path.expanduser("~"), "gpu_terminal"),
            _WORKSPACE,
        ) if path
    )


def source_home() -> str:
    """The first candidate workspace that exists.

    Falls back to the declared value when nothing exists, so callers keep
    reporting the path the operator configured rather than inventing one.
    """
    for path in candidates():
        if os.path.isdir(path):
            return path
    return candidates()[0]


def component_dir(relative: str) -> str:
    """Resolve one component directory across workspaces and both layouts.

    Returns the first existing directory; otherwise the preferred path, so an
    error message still names where the component was expected.
    """
    flat = os.path.basename(relative)
    for home in candidates():
        for tail in (relative, flat) if flat != relative else (relative,):
            path = os.path.join(home, tail)
            if os.path.isdir(path):
                return path
    return os.path.join(source_home(), relative)
