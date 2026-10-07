"""Read JSONL transcripts without loading them.

Every agent here appends newline-delimited JSON, and the interesting part of a
long conversation is always the end. Transcripts reach tens of megabytes, so
these read backward in bounded chunks and tolerate a truncated final record —
a transcript cut off mid-write is exactly the case this tool exists for.
"""
from __future__ import annotations

import json
import os
import time
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


# Hard limits for a bounded read: bytes read from the end, bytes held for one line, seconds.
TAIL_BYTES = 8 * 1024 * 1024
LINE_BYTES = 1024 * 1024
TAIL_SECONDS = 2.0


def bounded_reverse_lines(
    path: str,
    *,
    max_bytes: int = TAIL_BYTES,
    max_line: int = LINE_BYTES,
    max_seconds: float = TAIL_SECONDS,
    chunk: int = 65536,
) -> Iterator[bytes | None]:
    """Yield non-empty lines newest first under hard limits; `None` marks what was not read.

    At most `max_bytes` are read from the end of the file, never more than `max_line + chunk` bytes
    are held for one line, and reading stops after `max_seconds`. A line longer than `max_line` is
    never assembled: a `None` stands in for it and reading goes on past it. When the byte or time
    budget ends before the start of the file, a final `None` is yielded and the generator stops, so
    a caller can tell "the file ended" from "the rest was not read".
    """
    deadline = time.monotonic() + max_seconds
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        consumed = 0
        remainder = b""
        skipping = False        # inside a line longer than max_line, discarding back to its start
        while position > 0:
            if consumed >= max_bytes or time.monotonic() >= deadline:
                yield None
                return
            amount = min(chunk, position, max_bytes - consumed)
            position -= amount
            consumed += amount
            handle.seek(position)
            data = handle.read(amount)
            if skipping:
                segments = data.split(b"\n")
                if len(segments) == 1:
                    continue
                skipping = False
                data = b"\n".join(segments[:-1])        # the line's own start is dropped
                parts = data.split(b"\n")
                remainder = parts[0]
                lines = parts[1:]
            else:
                parts = (data + remainder).split(b"\n")
                remainder = parts[0]
                lines = parts[1:]
            for line in reversed(lines):
                if len(line) > max_line:
                    yield None
                elif line.strip():
                    yield line
            if len(remainder) > max_line:
                yield None
                remainder = b""
                skipping = True
        if remainder.strip() and not skipping:
            yield remainder


def head_records(path: str, limit: int = 64, *, max_line: int = LINE_BYTES) -> Iterator[dict]:
    """Yield the first parsed records, for the metadata agents write up front.

    A line longer than `max_line` ends the scan: the head is metadata, and nothing is assembled
    from an arbitrarily large record.
    """
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        for _ in range(limit):
            raw = handle.readline(max_line + 1)
            if not raw or (len(raw) > max_line and not raw.endswith(b"\n")):
                return
            record = load(raw)
            if record is not None:
                yield record


# The wider limits of a scan for display fields (not for a state): still bounded.
SCAN_BYTES = 128 * 1024 * 1024
SCAN_LINE = 16 * 1024 * 1024
SCAN_SECONDS = 15.0


def tail_records(path: str, limit: int | None = 200) -> Iterator[dict]:
    """Yield parsed records newest first.

    ``limit=None`` keeps memory bounded while allowing a caller to continue
    until it has recovered every required field from an unusually sparse
    transcript.
    """
    seen = 0
    for raw in bounded_reverse_lines(path, max_bytes=SCAN_BYTES, max_line=SCAN_LINE, max_seconds=SCAN_SECONDS):
        if raw is None:
            continue
        record = load(raw)
        if record is None:
            continue
        yield record
        seen += 1
        if limit is not None and seen >= limit:
            return


def tail_records_matching(
    path: str,
    needles: tuple[bytes, ...],
    limit: int | None = None,
) -> Iterator[dict]:
    """Yield newest records whose raw line contains one inexpensive marker.

    Agent logs can contain multi-megabyte tool results. A caller interested in
    turn boundaries should not UTF-8 decode and JSON-parse those payloads just
    to learn that their outer record type is irrelevant. The byte prefilter is
    deliberately only an optimization: a false positive is parsed normally,
    while a caller supplies every spelling it considers relevant.
    """
    seen = 0
    for raw in bounded_reverse_lines(path, max_bytes=SCAN_BYTES, max_line=SCAN_LINE, max_seconds=SCAN_SECONDS):
        if raw is None:
            continue
        if needles and not any(needle in raw for needle in needles):
            continue
        record = load(raw)
        if record is None:
            continue
        yield record
        seen += 1
        if limit is not None and seen >= limit:
            return


def condense(text: str, limit: int = 160) -> str:
    return " ".join(str(text).split())[:limit]
