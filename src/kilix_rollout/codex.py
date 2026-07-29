"""Codex rollouts.

One JSONL rollout per session under a dated directory tree. Codex states its
own turn boundaries: every turn emits `task_started` and, when it finishes,
`task_complete` — so a rollout whose latest turn event is `task_started` was
interrupted, with no inference needed.
"""
from __future__ import annotations

import os
import re

from . import jsonl, liveness, model
from .model import Session

_UUID = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex")


def _message_text(value) -> str:
    if isinstance(value, str):
        return jsonl.condense(value)
    if isinstance(value, list):
        pieces = []
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                candidate = item.get("text") or item.get("input_text")
                if isinstance(candidate, str):
                    pieces.append(candidate)
        return jsonl.condense(" ".join(pieces))
    return ""


def _rollouts(sessions_dir: str) -> list[str]:
    found: list[str] = []
    for current, _, names in os.walk(sessions_dir):
        for name in names:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                found.append(os.path.join(current, name))
    return found


#: Codex writes a record per tool call and per output chunk, so the last thing
#: the operator typed can sit a long way behind the end of a busy rollout. The
#: scan still stops as soon as it has everything — only prompt-less rollouts
#: pay for the depth.
_TAIL = 1200


def _inspect(path: str) -> tuple[str, str, str]:
    """Return (state, cwd, title) from the tail of a rollout."""
    state = ""
    cwd = ""
    prompt = ""
    for record in jsonl.tail_records(path, _TAIL):
        kind = record.get("type")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if kind == "turn_context" and not cwd:
            value = payload.get("cwd")
            if isinstance(value, str) and value:
                cwd = value
        elif kind == "event_msg":
            event = payload.get("type")
            if not state and event in ("task_started", "task_complete"):
                state = "cut-off" if event == "task_started" else "idle"
            if not prompt and event == "user_message":
                prompt = _message_text(payload.get("message") or payload.get("text"))
        if state and cwd and prompt:
            break
    if not cwd:
        for record in jsonl.head_records(path):
            if record.get("type") == "session_meta":
                payload = record.get("payload")
                if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                    cwd = payload["cwd"]
                break
    return state or "idle", cwd, prompt


def _session_id(path: str) -> str:
    matches = _UUID.findall(os.path.basename(path))
    if matches:
        return matches[-1].lower()
    for record in jsonl.head_records(path):
        if record.get("type") == "session_meta":
            payload = record.get("payload")
            if isinstance(payload, dict):
                value = payload.get("id") or payload.get("session_id")
                if isinstance(value, str):
                    return value.lower()
            break
    return ""


def discover(*, root: str = "", proc_root: str = "/proc",
             since: float = 0.0) -> list[Session]:
    base = root or home()
    oldest = model.cutoff(since)
    paths = []
    for path in _rollouts(os.path.join(base, "sessions")):
        try:
            updated = os.stat(path).st_mtime
        except OSError:
            continue
        if oldest and updated < oldest:
            continue              # stat-first, so old rollouts are never parsed
        paths.append((path, updated))

    owners = liveness.open_by([path for path, _ in paths], proc_root=proc_root)
    sessions: list[Session] = []
    for path, updated in paths:
        session_id = _session_id(path)
        if not session_id:
            continue
        state, cwd, title = _inspect(path)
        pids = owners.get(path, ())
        sessions.append(Session(
            provider="codex", session_id=session_id, path=path, cwd=cwd,
            title=title, updated=updated,
            state="live" if pids else state, pids=pids))
    return sessions


def resume_argv(session: Session, *, yolo: bool = False) -> list[str]:
    # Codex takes --yolo before the subcommand, not after it.
    argv = ["codex"]
    if yolo:
        argv.append("--yolo")
    argv.extend(("resume", session.session_id))
    return argv
