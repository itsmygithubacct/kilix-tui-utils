"""Decide whether a saved conversation still has a running owner.

Resuming a session a live process is still writing to would give the
conversation two writers, so every provider checks this before offering a
transcript for recovery.

Two signals, because the agents differ. Codex and Kimi hold their transcript
open for the life of the session, so an open descriptor in `/proc` is proof.
Claude Code instead publishes a descriptor file per running process, which is
cheaper to read but outlives a process that died without cleaning up — hence
the start-time check against the process table.
"""
from __future__ import annotations

import json
import os


def open_by(paths, *, proc_root: str = "/proc") -> dict[str, tuple[int, ...]]:
    """Map each path to the PIDs of our own processes holding it open."""
    wanted: dict[str, str] = {}
    for path in paths:
        try:
            wanted[os.path.realpath(path)] = path
        except OSError:
            wanted[os.path.abspath(path)] = path
    if not wanted:
        return {}

    found: dict[str, set[int]] = {}
    own = os.getuid()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return {}
    for entry in entries:
        if not entry.isdigit():
            continue
        descriptors = os.path.join(proc_root, entry, "fd")
        try:
            if os.stat(os.path.join(proc_root, entry)).st_uid != own:
                continue
            names = os.listdir(descriptors)
        except OSError:
            continue
        for name in names:
            try:
                target = os.readlink(os.path.join(descriptors, name))
            except OSError:
                continue
            if target.endswith(" (deleted)"):
                target = target[: -len(" (deleted)")]
            match = wanted.get(target)
            if match is not None:
                found.setdefault(match, set()).add(int(entry))
    return {path: tuple(sorted(pids)) for path, pids in found.items()}


MAX_PROC_BYTES = 1 << 16        # /proc/<pid>/stat is far smaller; a read that fills this is not complete
MAX_REGISTRY_BYTES = 1 << 20    # a registry descriptor is a few hundred bytes
MAX_PID = 1 << 22               # Linux's largest pid_max


def read_bounded(path: str, limit: int) -> bytes | None:
    """The whole file when it is at most `limit` bytes; None when unreadable or longer (never a prefix)."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    return None if len(data) > limit else data


def start_ticks(pid: int, *, proc_root: str = "/proc") -> str | None:
    """Return field 22 of /proc/<pid>/stat, the process start time."""
    data = read_bounded(os.path.join(proc_root, str(pid), "stat"), MAX_PROC_BYTES)
    if data is None:
        return None
    raw = data.decode("utf-8", errors="replace")      # only the numeric fields after the name are used
    # Field 2 is the comm name in parentheses and may itself contain spaces.
    closing = raw.rfind(")")
    if closing == -1:
        return None
    fields = raw[closing + 2:].split()
    return fields[19] if len(fields) > 19 else None


def start_time(pid: int, *, proc_root: str = "/proc") -> float:
    """The process start as a Unix time; 0.0 when it cannot be read."""
    ticks = start_ticks(pid, proc_root=proc_root)
    if ticks is None or not ticks.isdigit():
        return 0.0
    try:
        with open(os.path.join(proc_root, "stat"), encoding="utf-8") as handle:
            boot = next(float(line.split()[1]) for line in handle if line.startswith("btime "))
        return boot + int(ticks) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration):
        return 0.0


def registry_owners(directory: str, *, proc_root: str = "/proc") -> dict[str, tuple[int, ...]]:
    """Map session IDs to live PIDs from a directory of <pid>.json descriptors.

    An entry is believed only when the process still exists *and* started when
    the entry says it did, so a recycled process ID cannot resurrect a session.
    """
    owners: dict[str, set[int]] = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return {}
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        session_id = record.get("sessionId")
        try:
            pid = int(record.get("pid"))
        except (TypeError, ValueError):
            continue
        if not isinstance(session_id, str) or not session_id or pid <= 0:
            continue
        actual = start_ticks(pid, proc_root=proc_root)
        if actual is None:
            continue
        recorded = record.get("procStart")
        if recorded is not None and str(recorded) != actual:
            continue
        owners.setdefault(session_id.lower(), set()).add(pid)
    return {key: tuple(sorted(pids)) for key, pids in owners.items()}


def read_registry_file(path: str) -> dict | None:
    """A registry descriptor as an object; None if unreadable, too large, not UTF-8, not JSON (including
    nested or numeric forms the decoder cannot hold) or not an object. Never raises for a bad file."""
    data = read_bounded(path, MAX_REGISTRY_BYTES)
    if data is None:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError, OverflowError):
        return None
    return record if isinstance(record, dict) else None


def registry_row(record: dict) -> dict[str, object] | None:
    """A descriptor's identity fields, validated without coercion; None for any malformed value.

    `pid` must be a JSON integer in range. `procStart` (Claude writes it as a string of decimal digits)
    must be a non-negative JSON integer or such a string; absent is allowed here (callers that need it
    ask for it). A boolean, float, other string or an out-of-range number refuses the descriptor.
    """
    session_id = record.get("sessionId")
    pid = record.get("pid")
    if not isinstance(session_id, str) or not session_id:
        return None
    if type(pid) is not int or not 0 < pid <= MAX_PID:
        return None
    start = record.get("procStart")
    if start is None:
        text = ""
    elif type(start) is int and 0 <= start < 1 << 63:
        text = str(start)
    elif isinstance(start, str) and start.isascii() and start.isdigit() and len(start) <= 19:
        text = start
    else:
        return None
    return {
        "sessionId": session_id.lower(),
        "pid": pid,
        "procStart": text,
        "cwd": str(record.get("cwd") or ""),
        "status": str(record.get("status") or ""),
        "name": str(record.get("name") or ""),
        "version": str(record.get("version") or ""),
        "entrypoint": str(record.get("entrypoint") or ""),
    }


def registry_records(
    directory: str,
    *,
    proc_root: str = "/proc",
    require_start: bool = False,
) -> dict[str, tuple[dict[str, object], ...]]:
    """Return validated Claude registry metadata grouped by session ID.

    A descriptor is believed when its process exists and, if it records `procStart`, started
    then. `require_start` also refuses a descriptor without one: with no start time it cannot
    be told from a descriptor left behind for a pid that has since been reused. Malformed
    descriptors are skipped (`registry_row`), never raised.
    """
    found: dict[str, list[dict[str, object]]] = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return {}
    for name in names:
        if not name.endswith(".json"):
            continue
        record = read_registry_file(os.path.join(directory, name))
        row = registry_row(record) if record is not None else None
        if row is None:
            continue
        actual = start_ticks(row["pid"], proc_root=proc_root)
        recorded = row["procStart"]
        if actual is None or (recorded and recorded != actual):
            continue
        if require_start and not recorded:
            continue
        session_id = str(row.pop("sessionId"))
        found.setdefault(session_id, []).append(row)
    return {
        session_id: tuple(sorted(items, key=lambda item: int(item["pid"])))
        for session_id, items in found.items()
    }


def prune_registry(directory: str, *, proc_root: str = "/proc") -> list[dict[str, object]]:
    """Remove stale, structurally valid Claude registry descriptors.

    Malformed JSON is left alone for diagnosis.  A parsed descriptor is stale
    when its PID is gone, was recycled, or no longer matches its recorded
    process start time.
    """
    removed: list[dict[str, object]] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return removed
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        session_id = record.get("sessionId")
        try:
            pid = int(record.get("pid"))
        except (TypeError, ValueError):
            continue
        if not isinstance(session_id, str) or not session_id or pid <= 0:
            continue
        actual = start_ticks(pid, proc_root=proc_root)
        recorded = record.get("procStart")
        live = actual is not None and (
            recorded is None or str(recorded) == actual)
        if live:
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        removed.append({
            "path": path,
            "pid": pid,
            "session_id": session_id.lower(),
        })
    return removed
