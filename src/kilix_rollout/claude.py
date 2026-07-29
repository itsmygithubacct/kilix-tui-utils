"""Claude Code transcripts.

One JSONL file per conversation under a directory named after the working
directory, plus a descriptor file per running process. Claude Code appends
bookkeeping records (titles, mode changes, file-history snapshots) after a turn
ends, so the last line of a transcript is usually not the last thing that
happened — those are skipped when deciding whether a session was cut off.
"""
from __future__ import annotations

import os
import re

from . import jsonl, liveness, model
from .model import Session

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_BOOKKEEPING = frozenset({
    "agent-name", "ai-title", "attachment", "custom-title", "file-history-delta",
    "file-history-snapshot", "last-prompt", "mode", "permission-mode",
    "queue-operation",
})
_ABORTED = "[request interrupted"
_WRAPPED = re.compile(
    r"<(system-reminder|task-notification|local-command-stdout)>.*?</\1>", re.S)
_COMMAND = re.compile(r"<command-(name|message|args)>(.*?)</command-\1>", re.S)


def home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")


def _text(value) -> str:
    """Pull operator-typed text out of a message, ignoring tool plumbing."""
    if isinstance(value, str):
        blocks = [{"type": "text", "text": value}]
    elif isinstance(value, list):
        blocks = [item for item in value if isinstance(item, dict)]
    else:
        return ""
    pieces = [block.get("text", "") for block in blocks
              if block.get("type") == "text" and isinstance(block.get("text"), str)]
    joined = _WRAPPED.sub(" ", " ".join(pieces))
    # A slash command is stored as a block of tags; show what was typed.
    name = re.search(r"<command-name>(.*?)</command-name>", joined, re.S)
    if name:
        args = re.search(r"<command-args>(.*?)</command-args>", joined, re.S)
        joined = " ".join(filter(None, (
            name.group(1).strip(),
            args.group(1).strip() if args else "",
            _COMMAND.sub(" ", joined).strip())))
    return jsonl.condense(joined)


def _classify(record: dict) -> str:
    """Map the last real conversation record to a recovery state."""
    kind = record.get("type")
    if kind == "assistant":
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return "cut-off"          # a tool call nothing ever answered
        return "idle"
    if kind == "user":
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
            return "cut-off"              # a result the assistant never answered
        text = _text(content)
        if not text or text.casefold().startswith(_ABORTED):
            return "idle"                 # the operator stopped it on purpose
        return "cut-off"
    return "idle"


def _inspect(path: str) -> tuple[str, str, str]:
    """Return (state, cwd, title) by reading backward from the end."""
    state = ""
    cwd = ""
    title = ""
    prompt = ""
    for record in jsonl.tail_records(path):
        kind = record.get("type")
        if kind == "custom-title" and not title:
            title = jsonl.condense(record.get("customTitle") or "", 120)
        elif kind in ("ai-title", "agent-name") and not title:
            title = jsonl.condense(
                record.get("aiTitle") or record.get("agentName") or "", 120)
        elif kind == "last-prompt" and not prompt:
            prompt = jsonl.condense(record.get("lastPrompt") or "")
        if kind in _BOOKKEEPING or kind not in ("user", "assistant", "system"):
            continue
        if record.get("isSidechain"):
            continue                      # subagent traffic, not this session
        if not state:
            state = _classify(record)
        if not cwd and isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        if kind == "user" and not prompt:
            prompt = _text(record.get("message", {}).get("content")
                           if isinstance(record.get("message"), dict) else None)
        if state and cwd and (title or prompt):
            break
    if not cwd:
        for record in jsonl.head_records(path):
            if isinstance(record.get("cwd"), str) and record["cwd"]:
                cwd = record["cwd"]
                break
    return state or "idle", cwd, title or prompt


def discover(*, root: str = "", proc_root: str = "/proc",
             since: float = 0.0) -> list[Session]:
    base = root or home()
    projects = os.path.join(base, "projects")
    owners = liveness.registry_owners(os.path.join(base, "sessions"),
                                      proc_root=proc_root)
    oldest = model.cutoff(since)
    sessions: list[Session] = []
    try:
        folders = sorted(os.listdir(projects))
    except OSError:
        return sessions
    for folder in folders:
        directory = os.path.join(projects, folder)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl") or not _UUID.match(name[:-6]):
                continue
            path = os.path.join(directory, name)
            try:
                updated = os.stat(path).st_mtime
            except OSError:
                continue
            if oldest and updated < oldest:
                continue          # stat-first, so old transcripts are never parsed
            session_id = name[:-6].lower()
            state, cwd, title = _inspect(path)
            pids = owners.get(session_id, ())
            sessions.append(Session(
                provider="claude", session_id=session_id, path=path, cwd=cwd,
                title=title, updated=updated,
                state="live" if pids else state, pids=pids))
    return sessions


def resume_argv(session: Session) -> list[str]:
    return ["claude", "--resume", session.session_id]
