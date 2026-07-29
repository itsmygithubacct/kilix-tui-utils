"""The one session record every agent's transcripts are flattened into."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Session:
    """A saved conversation from one of the coding agents.

    `state` is the recovery signal, not the agent's own vocabulary:

    - `live`      a running process still owns this conversation
    - `cut-off`   the transcript stops mid-turn — the strongest sign a
                  terminal disappeared rather than the operator leaving
    - `idle`      the last turn finished; resumable, but possibly a clean exit
    """

    provider: str
    session_id: str
    path: str
    cwd: str
    title: str
    updated: float
    state: str = "idle"
    pids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def project(self) -> str:
        name = self.cwd.rstrip("/").rsplit("/", 1)[-1] if self.cwd else ""
        return name or "(unknown)"

    @property
    def resumable(self) -> bool:
        return self.state in ("idle", "cut-off")

    @property
    def short_id(self) -> str:
        return self.session_id[:13]

    def age(self, *, now: float | None = None) -> str:
        seconds = max(0, int((time.time() if now is None else now) - self.updated))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"


def newest_first(sessions: list[Session]) -> list[Session]:
    return sorted(sessions, key=lambda item: item.updated, reverse=True)


#: Windows offered in the picker. Years of history accumulate — reading every
#: transcript ever written costs seconds, and almost none of it is recoverable
#: work — so discovery filters on modification time before parsing anything.
RANGES: tuple[tuple[str, float], ...] = (
    ("24h", 86400.0),
    ("7d", 7 * 86400.0),
    ("30d", 30 * 86400.0),
    ("all", 0.0),
)

#: Index into RANGES. A recovery tool is opened just after something was lost,
#: so the freshest window is both the most useful default and the fastest to
#: scan; the picker widens on its own when nothing recent turns up.
DEFAULT_RANGE = 0          # 24h


def cutoff(since: float, *, now: float | None = None) -> float:
    """Return the oldest mtime still in the window, or 0 for no limit."""
    if since <= 0:
        return 0.0
    return (time.time() if now is None else now) - since
