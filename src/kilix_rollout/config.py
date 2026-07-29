"""The stack-wide default for skipping agent approval prompts.

The setting lives in the one shared `settings.conf` every Kilix component
reads, so it is configured in Kilix-95's control panel alongside every other
preference rather than hidden in this tool. Reading it through the shared theme
helper means a bare checkout, an SSH session, or a machine without Kilix
installed still works — it just falls back to the safe answer.
"""
from __future__ import annotations

from kilix_tui import theme

#: Shared settings key, declared by kilix_sdk.settings.
YOLO_KEY = "KILIX_CODING_YOLO"

_TRUE = frozenset({"on", "1", "true", "yes"})


def yolo_default() -> bool:
    """Return whether resumed agents should skip their approval prompts.

    Off unless the shared setting says otherwise: an agent that acts without
    asking is the more surprising of the two behaviours, so it is never the
    answer given by a missing or unreadable configuration.
    """
    return str(theme.setting(YOLO_KEY, "off")).strip().casefold() in _TRUE


def yolo_flag(provider_key: str) -> str:
    """Return the flag that agent uses, for showing the user what will run."""
    return ("--dangerously-skip-permissions" if provider_key == "claude"
            else "--yolo")
