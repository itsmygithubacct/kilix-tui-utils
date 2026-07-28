"""Put the shared core on sys.path from any tool, however it was invoked.

Each tool is a standalone script so it can be symlinked onto PATH, run from the
checkout, or launched from a desktop menu. All three need the same import root.
"""
from __future__ import annotations

import os
import sys


def install() -> str:
    """Add <repo>/src to sys.path and return the repo root."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    src = os.path.join(root, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return root
