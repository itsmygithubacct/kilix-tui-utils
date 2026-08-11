"""Small OSC-52 clipboard support shared by text-native applications."""
from __future__ import annotations

import base64
import os

MAX_COPY_BYTES = 16 * 1024


def sequence(text: str) -> str:
    payload = text.encode("utf-8")
    if len(payload) > MAX_COPY_BYTES:
        raise ValueError(f"clipboard text exceeds {MAX_COPY_BYTES} bytes")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"\033]52;c;{encoded}\a"


def copy(text: str, *, tty: str = "/dev/tty") -> bool:
    """Copy through the controlling terminal; return False when unavailable."""
    try:
        payload = sequence(text).encode("ascii")
        descriptor = os.open(tty, os.O_WRONLY | getattr(os, "O_NOCTTY", 0))
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except (OSError, ValueError, UnicodeError):
        return False
    return True
