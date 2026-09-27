"""omp (oh-my-pi) sessions; qwen-omp is omp running a Qwen model.

omp writes one JSONL file per session under `agent/sessions/<cwd below home,
with "/" as "-">/<timestamp>_<id>.jsonl`. A `session` record holds the id and working
directory, a `title` record the title, and `message` records the turns:
an assistant message whose stopReason is `stop` with no tool call ends a
turn; a tool result, user/developer message, or custom async message after it
means the agent is still at work. A provider `error` stays `working` because
omp may be in an automatic retry backoff which is not persisted to JSONL.
omp does not record whether a tool call is running or awaiting approval, so a
pending call is `unknown`: it is neither idle nor safe to type into.
`terminal-sessions/<tty>` maps a terminal to the session it owns. It is
preferred over the ambiguous newest-file heuristic.

Readable by explicit key only: omp is not one of the agents this package
installs or resumes from its own menu (see providers.py).
"""
from __future__ import annotations

import os
import time
from datetime import datetime

from . import jsonl, model
from .model import Session


def home() -> str:
    return os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(
        os.path.expanduser("~"), ".omp", "agent")


def folder_for(cwd: str) -> str:
    home_dir = os.path.expanduser("~").rstrip(os.sep)
    relative = cwd[len(home_dir):] if cwd == home_dir or cwd.startswith(home_dir + os.sep) else cwd
    return relative.replace("/", "-") or "-"


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _stable_idle(path: str, record: dict, now: float | None) -> str:
    changed = _timestamp(record.get("timestamp"))
    if not changed:
        try:
            changed = os.stat(path).st_mtime
        except OSError:
            return "unknown"
    return "idle" if (time.time() if now is None else now) - changed >= 0.3 else "working"


def activity(path: str, *, now: float | None = None) -> str:
    """`working` or `idle` from the newest message; `unknown` when ambiguous."""
    for record in jsonl.tail_records(path, limit=200):
        kind = record.get("type")
        if kind == "custom_message":
            return "working"
        if kind != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            calls = [part for part in message.get("content") or []
                     if isinstance(part, dict) and part.get("type") == "toolCall"]
            reason = message.get("stopReason")
            if reason == "error":
                # OMP 18.3.2 persists the failed assistant message, then emits
                # auto_retry_start only in process.  Default backoff can reach
                # five minutes and quota-reset waits can be longer, so no
                # finite transcript debounce can safely infer idle here.
                return "working"
            if reason == "aborted":
                return _stable_idle(path, record, now)
            if calls or reason == "toolUse":
                return "unknown"
            return _stable_idle(path, record, now) if reason == "stop" else "working"
        if role in ("user", "developer", "toolResult", "bashExecution"):
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


def session_for_pid(pid: int, *, root: str = "", proc_root: str = "/proc",
                    after: float = 0.0) -> Session | None:
    """Return the session named by omp's terminal state for ``pid``.

    The mapping is trusted only when it points inside this omp home, matches
    the session header, and was updated after the process began.
    """
    if pid <= 0:
        return None
    base = root or home()
    try:
        terminal_path = os.readlink(os.path.join(proc_root, str(pid), "fd", "0"))
        terminal = terminal_path.removeprefix("/dev/").replace(os.sep, "-")
        with open(os.path.join(base, "terminal-sessions", terminal),
                  encoding="utf-8") as handle:
            lines = [line.strip() for line in handle.read(4096).splitlines()[:4]]
    except OSError:
        return None
    if len(lines) < 2 or not lines[1]:
        return None
    path = os.path.realpath(lines[1])
    sessions_root = os.path.realpath(os.path.join(base, "sessions"))
    if os.path.commonpath((sessions_root, path)) != sessions_root:
        return None
    try:
        updated = os.stat(path).st_mtime
    except OSError:
        return None
    if after and updated < after:
        return None
    session_id, cwd, title = _header(path)
    if not session_id or (lines[0] and cwd != lines[0]):
        return None
    return Session(provider="omp", session_id=session_id, path=path, cwd=cwd,
                   title=jsonl.condense(title, 120), updated=updated,
                   state="cut-off" if activity(path) != "idle" else "idle")


def has_terminal_marker(pid: int, *, root: str = "", proc_root: str = "/proc",
                        after: float = 0.0) -> bool:
    """Whether this tty has an omp breadcrumb created since the process began.

    The target may not exist yet: omp writes the breadcrumb before lazily
    creating a first-turn session file.  Its presence forbids cwd fallback,
    including when malformed or pointing outside the sessions directory.
    """
    if pid <= 0:
        return False
    base = root or home()
    try:
        terminal_path = os.readlink(os.path.join(proc_root, str(pid), "fd", "0"))
        terminal = terminal_path.removeprefix("/dev/").replace(os.sep, "-")
        marker = os.path.join(base, "terminal-sessions", terminal)
        return not after or os.stat(marker).st_mtime >= after
    except OSError:
        return False


def resume_argv(session: Session, *, yolo: bool = False, model_name: str = "") -> list[str]:
    argv = ["omp"]
    if model_name:
        argv.extend(("--model", model_name))
    if yolo:
        argv.append("--auto-approve")
    argv.append(f"--resume={session.session_id}")
    return argv
