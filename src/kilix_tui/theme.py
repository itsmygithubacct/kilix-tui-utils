"""Colours and metrics, read from the one shared settings file.

Every Kilix component — the terminal, both desktop providers, Pleb, and
Plebian-OS — already reads `~/.local/gpu_terminal/settings.conf`. These tools
join that contract rather than inventing a second place to configure them, so a
change made in any settings interface reaches all of them.

The SDK is imported when a Kilix checkout is reachable and its absence is not
fatal: a tool run from a bare checkout, over SSH, or on a machine without Kilix
installed still starts with the built-in defaults.
"""
from __future__ import annotations

import os
from typing import Any

_SDK: Any | None = None


def _sdk() -> Any | None:
    """Return `kilix_sdk.settings`, or None when Kilix is not reachable."""
    global _SDK
    if _SDK is not None:
        return _SDK or None
    import importlib
    import sys

    source_home = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or os.path.join(
        os.path.expanduser("~"), "gpu_terminal")
    candidates = [
        os.environ.get("KILIX_HOME", ""),
        os.path.join(os.path.abspath(os.path.expanduser(source_home)), "kilix"),
    ]
    for home in candidates:
        config = os.path.join(home, "config") if home else ""
        if config and os.path.isdir(os.path.join(config, "kilix_sdk")):
            if config not in sys.path:
                sys.path.insert(0, config)
            try:
                _SDK = importlib.import_module("kilix_sdk.settings")
                return _SDK
            except Exception:
                break
    _SDK = False  # type: ignore[assignment]
    return None


def setting(key: str, default: str) -> str:
    """Read one shared setting, falling back when Kilix is unavailable."""
    sdk = _sdk()
    if sdk is None:
        return os.environ.get(key, default)
    try:
        return sdk.load().get(key, default)
    except Exception:
        return default


# Tango-ish palette matching Kilix's own chrome. Curses colour numbers; tools
# that draw into a framebuffer use the RGB triples instead.
FG = 7
ACCENT = 4
WARN = 3
ALERT = 1
MUTED = 8

RGB = {
    "fg": (0xD3, 0xD7, 0xCF),
    "bg": (0x2E, 0x34, 0x36),
    "accent": (0x34, 0x65, 0xA4),
    "warn": (0xC4, 0xA0, 0x00),
    "alert": (0xCC, 0x00, 0x00),
    "muted": (0x55, 0x57, 0x53),
}
