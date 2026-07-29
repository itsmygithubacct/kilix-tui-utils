"""Read JSONL transcripts without loading them.

Every agent here appends newline-delimited JSON, and the interesting part of a
long conversation is always the end. Transcripts reach tens of megabytes, so
these read backward in bounded chunks and tolerate a truncated final record —
a transcript cut off mid-write is exactly the case this tool exists for.
"""
from __future__ import annotations

import json
import os
from typing import Iterator


def load(raw: bytes) -> dict | None:
    """Parse one line, returning None for anything that is not an object."""
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def reverse_lines(path: str, *, chunk: int = 65536) -> Iterator[bytes]:
    """Yield a file's non-empty lines newest first, memory-bounded."""
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position > 0:
            amount = min(chunk, position)
            position -= amount
            handle.seek(position)
            parts = (handle.read(amount) + remainder).split(b"\n")
            remainder = parts[0]
            for line in reversed(parts[1:]):
                if line.strip():
                    yield line
        if remainder.strip():
            yield remainder


def head_records(path: str, limit: int = 64) -> Iterator[dict]:
    """Yield the first parsed records, for the metadata agents write up front."""
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        for _, raw in zip(range(limit), handle):
            record = load(raw)
            if record is not None:
                yield record


def tail_records(path: str, limit: int = 200) -> Iterator[dict]:
    """Yield parsed records newest first, bounded so a huge file stays cheap."""
    seen = 0
    for raw in reverse_lines(path):
        record = load(raw)
        if record is None:
            continue
        yield record
        seen += 1
        if seen >= limit:
            return


def condense(text: str, limit: int = 160) -> str:
    return " ".join(str(text).split())[:limit]
