"""Grok Build sessions.

Grok keeps a directory per session under `sessions/<url-encoded cwd>/<id>/`.
`events.jsonl` is its own event log: `turn_started` / `turn_ended` bound each
turn, and `permission_requested` / `permission_resolved` bound a question to
the user. `active_sessions.json` at the top level lists the sessions a live
process holds, with that process's pid.

Readable by explicit key only: grok is not one of the agents this package
installs or resumes from its own menu (see providers.py).
"""
from __future__ import annotations

import json
import os
from urllib.parse import unquote

from . import jsonl, model
from .model import Session


def home() -> str:
    return os.environ.get("GROK_HOME") or os.path.join(os.path.expanduser("~"), ".grok")


def _alive(pid: int, proc_root: str) -> bool:
    try:
        return os.stat(os.path.join(proc_root, str(pid))).st_uid == os.getuid()
    except OSError:
        return False


def active(base: str = "", *, proc_root: str = "/proc") -> dict[str, tuple[int, str]]:
    """session id -> (pid, cwd) for sessions a live process of this user holds."""
    try:
        with open(os.path.join(base or home(), "active_sessions.json"), encoding="utf-8") as handle:
            records = json.load(handle)
    except (OSError, ValueError):
        return {}
    found = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        pid, session_id = record.get("pid"), record.get("session_id")
        if isinstance(pid, int) and isinstance(session_id, str) and _alive(pid, proc_root):
            found[session_id] = (pid, str(record.get("cwd") or ""))
    return found


def activity(session_dir: str) -> str:
    """`working`, `waiting` or `idle` from the newest turn and permission events;
    `unknown` when the log says nothing yet."""
    for record in jsonl.tail_records(os.path.join(session_dir, "events.jsonl"), limit=400):
        kind = record.get("type")
        if kind == "permission_requested":
            return "waiting"
        if kind in ("permission_resolved", "turn_started", "tool_started",
                    "tool_completed", "first_token", "loop_started"):
            return "working"
        if kind == "turn_ended":
            return "idle"
    return "unknown"


def _title(session_dir: str) -> str:
    for record in jsonl.head_records(os.path.join(session_dir, "chat_history.jsonl"), limit=64):
        if record.get("type") != "user" or record.get("synthetic_reason"):
            continue
        content = record.get("content")
        parts = content if isinstance(content, list) else [{"text": content}]
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text.strip() and not text.lstrip().startswith("<"):
                return jsonl.condense(text, 120)
    return ""


def discover(*, root: str = "", proc_root: str = "/proc", since: float = 0.0,
             selectors: tuple[str, ...] = ()) -> list[Session]:
    base = root or home()
    oldest = model.cutoff(since)
    live = active(base, proc_root=proc_root)
    sessions: list[Session] = []
    top = os.path.join(base, "sessions")
    try:
        folders = os.listdir(top)
    except OSError:
        return []
    for folder in folders:
        cwd = unquote(folder)
        try:
            ids = os.listdir(os.path.join(top, folder))
        except OSError:
            continue
        for session_id in ids:
            directory = os.path.join(top, folder, session_id)
            events = os.path.join(directory, "events.jsonl")
            if selectors and not any(session_id.casefold().startswith(s.strip().casefold())
                                     for s in selectors if s.strip()):
                continue
            try:
                updated = os.stat(events).st_mtime
            except OSError:
                continue
            if oldest and updated < oldest:
                continue
            now = activity(directory)
            pid = live.get(session_id, (0, ""))[0]
            state = "live" if pid else ("cut-off" if now in ("working", "waiting") else "idle")
            sessions.append(Session(
                provider="grok", session_id=session_id, path=events, cwd=cwd,
                title=_title(directory), updated=updated, state=state,
                pids=(pid,) if pid else (), live_status=now if pid else ""))
    return sessions


def session_dir(session_id: str, cwd: str, base: str = "") -> str:
    from urllib.parse import quote
    return os.path.join(base or home(), "sessions", quote(cwd, safe=""), session_id)


def resume_argv(session: Session, *, yolo: bool = False) -> list[str]:
    argv = ["grok"]
    if yolo:
        argv.append("--always-approve")
    argv.extend(("--resume", session.session_id))
    return argv
