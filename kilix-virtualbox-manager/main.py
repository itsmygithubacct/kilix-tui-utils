#!/usr/bin/env python3
"""Launch the Kilix VirtualBox VPN manager from its source checkout."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kilix_virtualbox_manager.cli import main  # noqa: E402
from kilix_virtualbox_manager.tui import State, handle, render  # noqa: E402,F401


if __name__ == "__main__":
    raise SystemExit(main())
