"""Kimi Code sessions.

Kimi keeps a directory per session rather than a single file: `state.json`
holds the title and working directory, and the conversation itself is a wire
log under `agents/main/`. An index at the top level maps session IDs to those
directories, which is authoritative — the directory names are workspace hashes
and cannot be decoded.

Turn boundaries live inside the wire log's loop events: a `step.begin` with no
matching `step.end` after it means the session stopped mid-step.
"""
from __future__ import annotations

import json
import os

from . import jsonl, liveness, model
from .model import Session

_PLACEHOLDER_TITLES = frozenset({"", "new session", "untitled"})


def home() -> str:
    return os.environ.get("KIMI_CODE_HOME") or os.path.join(
        os.path.expanduser("~"), ".kimi-code")


def wire_path(session_dir: str) -> str:
    return os.path.join(session_dir, "agents", "main", "wire.jsonl")


def _index(base: str) -> list[dict]:
    """Read the session index, keeping the last entry for each session ID."""
    path = os.path.join(base, "session_index.jsonl")
    entries: dict[str, dict] = {}
    try:
        handle = open(path, "rb")
    except OSError:
        return []
    with handle:
        for raw in handle:
            record = jsonl.load(raw)
            if not record:
                continue
            session_id = record.get("sessionId")
            directory = record.get("sessionDir")
            if isinstance(session_id, str) and isinstance(directory, str):
                entries[session_id] = record
    return list(entries.values())


def _state_file(session_dir: str) -> dict:
    try:
        with open(os.path.join(session_dir, "state.json"), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _inspect(path: str) -> tuple[str, str]:
    """Return (state, last prompt) from a wire log."""
    state = ""
    prompt = ""
    for record in jsonl.tail_records(path):
        kind = record.get("type")
        if kind == "context.append_loop_event" and not state:
            event = record.get("event")
            event_type = event.get("type") if isinstance(event, dict) else None
            if event_type == "step.end":
                state = "idle"
            elif event_type == "step.begin":
                state = "cut-off"
        elif kind == "turn.prompt":
            if not prompt:
                prompt = _prompt_text(record.get("input"))
            if not state:
                state = "cut-off"         # a prompt no step ever answered
        if state and prompt:
            break
    return state or "idle", prompt


def _prompt_text(value) -> str:
    if isinstance(value, str):
        return jsonl.condense(value)
    if isinstance(value, list):
        pieces = [item.get("text", "") for item in value
                  if isinstance(item, dict) and isinstance(item.get("text"), str)]
        return jsonl.condense(" ".join(pieces))
    return ""


def discover(*, root: str = "", proc_root: str = "/proc",
             since: float = 0.0,
             selectors: tuple[str, ...] = ()) -> list[Session]:
    base = root or home()
    oldest = model.cutoff(since)
    entries = _index(base)
    if selectors:
        selected = []
        for entry in entries:
            session_id = str(entry.get("sessionId") or "")
            directory = str(entry.get("sessionDir") or "")
            wire = wire_path(directory)
            absolute = os.path.abspath(wire).casefold()
            name = os.path.basename(wire).casefold()
            if any(
                session_id.casefold().startswith(selector.strip().casefold())
                or name == selector.strip().casefold()
                or absolute == os.path.abspath(
                    os.path.expanduser(selector)).casefold()
                for selector in selectors
                if selector.strip()
            ):
                selected.append(entry)
        entries = selected
    wires = [wire_path(str(entry.get("sessionDir"))) for entry in entries]
    owners = liveness.open_by(wires, proc_root=proc_root)

    sessions: list[Session] = []
    for entry in entries:
        session_id = str(entry.get("sessionId") or "")
        directory = str(entry.get("sessionDir") or "")
        if not session_id or not os.path.isdir(directory):
            continue
        wire = wire_path(directory)
        saved = _state_file(directory)
        cwd = str(saved.get("workDir") or entry.get("workDir") or "")
        try:
            updated = os.stat(wire).st_mtime
        except OSError:
            try:
                updated = os.stat(directory).st_mtime
            except OSError:
                continue
        if oldest and updated < oldest:
            continue              # stat-first, so old sessions are never parsed
        state, prompt = _inspect(wire)
        title = str(saved.get("title") or "")
        if title.strip().casefold() in _PLACEHOLDER_TITLES:
            title = prompt
        pids = owners.get(wire, ())
        sessions.append(Session(
            provider="kimi", session_id=session_id, path=wire, cwd=cwd,
            title=jsonl.condense(title, 120), updated=updated,
            state="live" if pids else state, pids=pids))
    return sessions


def resume_argv(session: Session, *, yolo: bool = False) -> list[str]:
    argv = ["kimi"]
    if yolo:
        argv.append("--yolo")
    argv.extend(("--session", session.session_id))
    return argv
