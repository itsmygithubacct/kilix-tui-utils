"""The desktop's one durable record: recent launches and pinned Home rows.

The desk was deliberately stateless — it composed and read, and persisted
nothing, so no session could inherit a surprise from the last one. That
purity had a real cost: nothing you launch is ever offered again, and Home
could carry nothing of yours. The decision, recorded here because it breaks
a stated rule: the desktop now writes exactly one small JSON file, and only
this module writes it.

What it holds: the launches worth offering again (`recents`, capped and
most-recent-first) and the rows the user pinned to Home (`pinned`). What was
declined: restoring the last place on start. A desktop that opens somewhere
different every session is a desktop whose first keystroke is unpredictable;
Home stays the fixed landing, and the recents ON Home are how the last
session's work stays one Enter away.

The mechanics follow the stack's durable-state pattern without importing it
(kilix-tui stays SDK-free): a validated record, an atomic replace, and
write-on-change only — reading the desktop costs no writes, a record that
did not change costs no I/O, and a filesystem that refuses is a desktop that
merely forgets, never one that crashes. Confirmed actions (power, updates)
and quiet toggles are never recorded: a recents row must be safe to re-run
on one Enter.

The file lives under `$XDG_STATE_HOME/kilix-tui/desk.json` (state, not
config: losing it loses convenience, no behavior). `KILIX_TUI_STATE` points
it elsewhere, which is also how the tests keep their hands off the real one.
"""
from __future__ import annotations

import json
import os
from typing import Sequence

MAX_RECENTS = 8


def state_path() -> str:
    if override := os.environ.get("KILIX_TUI_STATE"):
        return override
    base = (os.environ.get("XDG_STATE_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "state"))
    return os.path.join(base, "kilix-tui", "desk.json")


def _rows(value: object) -> list[dict]:
    """Only well-formed rows survive a read: a hand-edited or damaged file
    degrades to fewer rows, never to a crash somewhere in a render."""
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for row in value:
        if not isinstance(row, dict):
            continue
        label = row.get("label")
        argv = row.get("argv")
        if (isinstance(label, str) and label
                and isinstance(argv, list) and argv
                and all(isinstance(part, str) for part in argv)):
            out.append({"label": label, "argv": list(argv)})
    return out


def load() -> dict:
    """The record, whole and valid; a missing or corrupt file is a fresh
    start, not an error."""
    try:
        with open(state_path(), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    return {"recents": _rows(record.get("recents"))[:MAX_RECENTS],
            "pinned": _rows(record.get("pinned"))}


def save(record: dict) -> None:
    """Write the record — atomically, and only when it actually changed."""
    if record == load():
        return
    path = state_path()
    swap = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(swap, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1)
            handle.write("\n")
        os.replace(swap, path)
    except OSError:
        pass                 # a desktop that cannot persist still runs


def _row(label: str, argv: Sequence[str]) -> dict:
    return {"label": str(label), "argv": [str(part) for part in argv]}


def recents() -> list[dict]:
    return load()["recents"]


def pinned() -> list[dict]:
    return load()["pinned"]


def remember_launch(label: str, argv: Sequence[str]) -> None:
    """Move `label` to the front of the recents, keeping the cap."""
    record = load()
    record["recents"] = [_row(label, argv)] + [
        row for row in record["recents"] if row["label"] != str(label)
    ][:MAX_RECENTS - 1]
    save(record)


def toggle_pin(label: str, argv: Sequence[str]) -> bool:
    """Pin `label` to Home, or unpin it when already there; True is pinned."""
    record = load()
    kept = [row for row in record["pinned"] if row["label"] != str(label)]
    now_pinned = len(kept) == len(record["pinned"])
    if now_pinned:
        kept.append(_row(label, argv))
    record["pinned"] = kept
    save(record)
    return now_pinned
