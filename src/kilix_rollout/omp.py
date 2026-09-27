"""omp (oh-my-pi) sessions; qwen-omp is omp running a Qwen model.

omp writes one JSONL file per session under `agent/sessions/<cwd with "/" as
"-">/<timestamp>_<id>.jsonl`. A `session` record holds the id and working
directory, a `title` record the title, and `message` records the turns:
an assistant message whose stopReason is `stop` with no tool call ends a
turn; `toolUse`, a tool result or a user message after it means the agent is
still at work. omp does not keep the file open and has no registry of live
processes, so liveness comes from the pane: an omp process whose working
directory and start time match the newest session there.

Readable by explicit key only: omp is not one of the agents this package
installs or resumes from its own menu (see providers.py).
"""
from __future__ import annotations

import os

from . import jsonl, model
from .model import Session


def home() -> str:
    return os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(
        os.path.expanduser("~"), ".omp", "agent")


def folder_for(cwd: str) -> str:
    return cwd.replace("/", "-")


def activity(path: str) -> str:
    """`working` or `idle` from the newest message; `unknown` with none."""
    for record in jsonl.tail_records(path, limit=200):
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            calls = [part for part in message.get("content") or []
                     if isinstance(part, dict) and part.get("type") == "toolCall"]
            return "idle" if message.get("stopReason") == "stop" and not calls else "working"
        if role in ("user", "toolResult"):
            return "working"
    return "unknown"


def _header(path: str) -> tuple[str, str, str]:
    """(session id, cwd, title) from the first records."""
    session_id = cwd = title = ""
    for record in jsonl.head_records(path, limit=16):
        kind = record.get("type")
        if kind == "session":
            session_id = str(record.get("id") or "")
            cwd = str(record.get("cwd") or "")
            title = title or str(record.get("title") or "")
        elif kind == "title" and record.get("title"):
            title = str(record["title"])
    return session_id, cwd, title


def discover(*, root: str = "", proc_root: str = "/proc", since: float = 0.0,
             selectors: tuple[str, ...] = ()) -> list[Session]:
    base = os.path.join(root or home(), "sessions")
    oldest = model.cutoff(since)
    sessions: list[Session] = []
    try:
        folders = os.listdir(base)
    except OSError:
        return []
    for folder in folders:
        try:
            names = os.listdir(os.path.join(base, folder))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(base, folder, name)
            try:
                updated = os.stat(path).st_mtime
            except OSError:
                continue
            if oldest and updated < oldest:
                continue
            session_id, cwd, title = _header(path)
            if not session_id:
                continue
            if selectors and not any(session_id.casefold().startswith(s.strip().casefold())
                                     for s in selectors if s.strip()):
                continue
            now = activity(path)
            sessions.append(Session(
                provider="omp", session_id=session_id, path=path, cwd=cwd,
                title=jsonl.condense(title, 120), updated=updated,
                state="cut-off" if now == "working" else "idle"))
    return sessions


def newest_in(cwd: str, *, root: str = "", after: float = 0.0) -> Session | None:
    """The newest session file for a working directory, written at or after
    `after` (a process start time); None when there is none."""
    candidates = [s for s in discover(root=root) if s.cwd == cwd and s.updated >= after]
    return max(candidates, key=lambda s: s.updated) if candidates else None


def resume_argv(session: Session, *, yolo: bool = False, model_name: str = "") -> list[str]:
    argv = ["omp"]
    if model_name:
        argv.extend(("--model", model_name))
    if yolo:
        argv.append("--auto-approve")
    argv.append(f"--resume={session.session_id}")
    return argv
