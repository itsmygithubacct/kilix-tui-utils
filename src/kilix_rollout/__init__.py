"""Recover coding-agent sessions whose terminal went away.

Claude Code, Codex, and Kimi Code all persist a conversation to disk as it
happens, and all three can reload one by ID. Losing the terminal ends the
process without touching those files, so a vanished session is still on disk
and still resumable — the hard part is only ever finding it.

Each agent stores its transcripts differently, so `claude`, `codex`, and `kimi`
own the per-agent details and `providers` presents them as one list.
"""
from __future__ import annotations

__all__ = ["PROVIDERS", "discover", "provider"]

from .providers import PROVIDERS, discover, provider
