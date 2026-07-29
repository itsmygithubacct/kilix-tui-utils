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


def start_ticks(pid: int, *, proc_root: str = "/proc") -> str | None:
    """Return field 22 of /proc/<pid>/stat, the process start time."""
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return None
    # Field 2 is the comm name in parentheses and may itself contain spaces.
    closing = raw.rfind(")")
    if closing == -1:
        return None
    fields = raw[closing + 2:].split()
    return fields[19] if len(fields) > 19 else None


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
