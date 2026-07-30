"""Claude Code transcript discovery and recovery metadata.

Claude stores one JSONL conversation per session below ``projects`` and keeps
side-car directories for tool results, subagents, and workflow state.  The
side-cars can outlive the transcript, so discovery reports them as orphaned
instead of silently pretending that ``claude --resume`` can still load them.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re

from . import jsonl, liveness, model
from .model import Session

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
_BOOKKEEPING = frozenset({
    "agent-name", "ai-title", "attachment", "custom-title",
    "file-history-delta", "file-history-snapshot", "last-prompt", "mode",
    "permission-mode", "queue-operation",
})
_ABORTED = "[request interrupted"
_WRAPPED = re.compile(
    r"<(system-reminder|task-notification|local-command-stdout)>.*?</\1>", re.S)
_COMMAND = re.compile(r"<command-(name|message|args)>(.*?)</command-\1>", re.S)


def home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _blocks(record: dict) -> list[dict]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    return []


def _text(value) -> str:
    """Pull operator-typed text out of a message, ignoring tool plumbing."""
    if isinstance(value, str):
        blocks = [{"type": "text", "text": value}]
    elif isinstance(value, list):
        blocks = [item for item in value if isinstance(item, dict)]
    else:
        return ""
    pieces = [
        block.get("text", "") for block in blocks
        if block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    joined = _WRAPPED.sub(" ", " ".join(pieces))
    name = re.search(r"<command-name>(.*?)</command-name>", joined, re.S)
    if name:
        args = re.search(r"<command-args>(.*?)</command-args>", joined, re.S)
        joined = " ".join(filter(None, (
            name.group(1).strip(),
            args.group(1).strip() if args else "",
            _COMMAND.sub(" ", joined).strip(),
        )))
    return jsonl.condense(joined, 500)


def _assistant_text(record: dict) -> str:
    message = record.get("message")
    return _text(message.get("content") if isinstance(message, dict) else None)


def _user_text(record: dict) -> str:
    """Return operator text while ignoring Claude's synthetic meta messages."""
    if record.get("isMeta"):
        return ""
    message = record.get("message")
    return _text(message.get("content") if isinstance(message, dict) else None)


def _pending_tool(record: dict) -> str:
    for block in _blocks(record):
        if block.get("type") == "tool_use":
            return str(block.get("name") or "tool")
    return ""


def _classify(record: dict) -> tuple[str, str]:
    """Map the last real conversation record to ``(state, pending_tool)``."""
    kind = record.get("type")
    if kind == "assistant":
        pending = _pending_tool(record)
        return ("cut-off", pending) if pending else ("idle", "")
    if kind == "user":
        blocks = _blocks(record)
        if any(block.get("type") == "tool_result" for block in blocks):
            return "cut-off", ""
        text = _user_text(record)
        if not text or text.casefold().startswith(_ABORTED):
            return "idle", ""
        return "cut-off", ""
    return "idle", ""


def _head(path: str) -> dict[str, object]:
    details: dict[str, object] = {}
    for record in jsonl.head_records(path):
        value = record.get("sessionId")
        if "session_id" not in details and isinstance(value, str) and value:
            details["session_id"] = value.lower()
        for key, field in (
            ("cwd", "cwd"),
            ("version", "version"),
            ("entrypoint", "entrypoint"),
        ):
            value = record.get(field)
            if key not in details and isinstance(value, str) and value:
                details[key] = value
        if "started" not in details:
            started = _timestamp(record.get("timestamp"))
            if started is not None:
                details["started"] = started
    return details


def _tail(path: str) -> dict[str, str]:
    details = {
        "state": "",
        "pending_tool": "",
        "last_turn_event": "",
        "cwd": "",
        "git_branch": "",
        "version": "",
        "title": "",
        "last_user_message": "",
        "last_agent_message": "",
    }
    generated_title = ""
    last_prompt = ""
    for record in jsonl.tail_records(path, None):
        kind = record.get("type")
        if kind == "custom-title" and not details["title"]:
            value = record.get("customTitle")
            if isinstance(value, str) and value.strip():
                details["title"] = jsonl.condense(_text(value), 120)
        elif kind in ("ai-title", "agent-name") and not generated_title:
            value = record.get("aiTitle") or record.get("agentName")
            if isinstance(value, str) and value.strip():
                generated_title = jsonl.condense(_text(value), 120)
        elif kind == "last-prompt" and not last_prompt:
            value = record.get("lastPrompt")
            if isinstance(value, str) and value.strip():
                last_prompt = _text(value)

        if kind in _BOOKKEEPING or kind not in ("user", "assistant", "system"):
            continue
        if record.get("isSidechain"):
            continue
        if not details["state"]:
            state, pending = _classify(record)
            details["state"] = state
            details["pending_tool"] = pending
            details["last_turn_event"] = str(kind)
        for key, field in (
            ("cwd", "cwd"),
            ("git_branch", "gitBranch"),
            ("version", "version"),
        ):
            value = record.get(field)
            if not details[key] and isinstance(value, str) and value:
                details[key] = value
        if kind == "user" and not details["last_user_message"]:
            details["last_user_message"] = _user_text(record)
        elif kind == "assistant" and not details["last_agent_message"]:
            details["last_agent_message"] = _assistant_text(record)
        if all(details[key] for key in (
            "cwd", "git_branch", "last_user_message", "last_agent_message",
        )):
            break
    details["title"] = details["title"] or generated_title
    details["last_user_message"] = (
        details["last_user_message"] or last_prompt)
    return details


def decode_project_dir(
    name: str,
    *,
    root: Path = Path("/"),
    budget: int = 4096,
) -> Path | None:
    """Best-effort recovery of a path encoded into Claude's project name."""
    if not name.startswith("-"):
        return None
    tokens = name[1:].split("-")
    if not tokens:
        return None
    remaining = budget
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, index = stack.pop()
        if index == len(tokens):
            return directory
        remaining -= 1
        if remaining <= 0:
            return None
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.is_symlink():
                continue
            parts = re.sub(r"[._]", "-", child.name).split("-")
            width = len(parts)
            if tokens[index:index + width] == parts:
                stack.append((child, index + width))
    return None


def _project_cwd(directory: Path) -> str:
    try:
        siblings = sorted(directory.glob("*.jsonl"))
    except OSError:
        siblings = []
    for sibling in siblings:
        value = _head(str(sibling)).get("cwd")
        if isinstance(value, str) and value:
            return value
    inferred = decode_project_dir(directory.name)
    return str(inferred) if inferred is not None else ""


def _orphaned(
    projects: Path,
    known_ids: set[str],
    *,
    oldest: float,
    selectors: tuple[str, ...] = (),
) -> list[Session]:
    found: dict[str, Session] = {}
    try:
        project_dirs = sorted(projects.iterdir())
    except OSError:
        return []
    for project in project_dirs:
        if not project.is_dir():
            continue
        cwd = ""
        resolved = False
        try:
            children = sorted(project.iterdir())
        except OSError:
            continue
        for child in children:
            session_id = child.name.lower()
            if (not child.is_dir() or not _UUID.match(session_id)
                    or session_id in known_ids
                    or child.with_suffix(".jsonl").is_file()):
                continue
            if selectors and not _matches_selector(
                    child, session_id=session_id, selectors=selectors):
                continue
            try:
                updated = child.stat().st_mtime
            except OSError:
                continue
            if oldest and updated < oldest:
                continue
            if not resolved:
                cwd = _project_cwd(project)
                resolved = True
            record = Session(
                provider="claude",
                session_id=session_id,
                path=str(child),
                cwd=cwd,
                original_cwd=cwd,
                title="",
                updated=updated,
                state="orphaned",
                invalid_reason="the transcript is gone; only side-car data remains",
            )
            previous = found.get(session_id)
            if previous is None or (
                (not previous.cwd and record.cwd)
                or record.updated > previous.updated
            ):
                found[session_id] = record
    return list(found.values())


def _matches_selector(
    path: Path,
    *,
    session_id: str,
    selectors: tuple[str, ...],
) -> bool:
    absolute = str(path.absolute()).casefold()
    name = path.name.casefold()
    for selector in selectors:
        needle = selector.strip().casefold()
        if not needle:
            continue
        expanded = str(Path(selector).expanduser().absolute()).casefold()
        if (session_id.casefold().startswith(needle)
                or name == needle
                or absolute == expanded):
            return True
    return False


def discover(
    *,
    root: str = "",
    projects_root: str = "",
    registry_root: str = "",
    include_orphans: bool = True,
    proc_root: str = "/proc",
    since: float = 0.0,
    selectors: tuple[str, ...] = (),
) -> list[Session]:
    """Discover transcripts below a Claude home or an explicit projects root."""
    base = Path(root or home()).expanduser()
    projects = Path(projects_root).expanduser() if projects_root else base / "projects"
    registry = Path(registry_root).expanduser() if registry_root else base / "sessions"
    live_records = liveness.registry_records(
        str(registry), proc_root=proc_root)
    oldest = model.cutoff(since)
    sessions: list[Session] = []
    known_ids: set[str] = set()
    try:
        folders = sorted(projects.iterdir())
    except OSError:
        folders = []
    for directory in folders:
        if not directory.is_dir():
            continue
        try:
            paths = sorted(directory.glob("*.jsonl"))
        except OSError:
            continue
        for path in paths:
            try:
                updated = path.stat().st_mtime
            except OSError:
                continue
            if oldest and updated < oldest:
                continue
            filename_id = (
                path.stem.lower() if _UUID.match(path.stem) else "")
            if (selectors and filename_id
                    and not _matches_selector(
                        path, session_id=filename_id, selectors=selectors)):
                # Standard Claude filenames are authoritative, so unmatched
                # files need no JSON parsing during targeted resolution.
                continue
            head = _head(str(path))
            session_id = (
                filename_id
                or str(head.get("session_id") or "").lower()
            )
            if selectors and not _matches_selector(
                    path, session_id=session_id, selectors=selectors):
                continue
            display_id = session_id or path.stem
            tail = _tail(str(path))
            live = live_records.get(session_id, ()) if session_id else ()
            pids = tuple(int(item["pid"]) for item in live)
            state = (
                "invalid" if not session_id else
                "live" if pids else
                tail["state"] or "idle"
            )
            first_live = live[0] if live else {}
            cwd = (
                tail["cwd"]
                or str(head.get("cwd") or "")
                or str(first_live.get("cwd") or "")
            )
            sessions.append(Session(
                provider="claude",
                session_id=display_id,
                path=str(path),
                cwd=cwd,
                original_cwd=str(head.get("cwd") or ""),
                title=tail["title"] or tail["last_user_message"],
                updated=updated,
                state=state,
                pids=pids,
                live_status=str(first_live.get("status") or ""),
                started=(head.get("started")
                         if isinstance(head.get("started"), float) else None),
                last_user_message=tail["last_user_message"],
                last_agent_message=tail["last_agent_message"],
                last_turn_event=tail["last_turn_event"],
                pending_tool=tail["pending_tool"],
                git_branch=tail["git_branch"],
                version=(
                    tail["version"]
                    or str(head.get("version") or "")
                    or str(first_live.get("version") or "")
                ),
                entrypoint=(
                    str(head.get("entrypoint") or "")
                    or str(first_live.get("entrypoint") or "")
                ),
                invalid_reason=(
                    "no session ID was found in the filename or transcript"
                    if not session_id else ""),
            ))
            if session_id:
                known_ids.add(session_id)
    if include_orphans:
        sessions.extend(_orphaned(
            projects, known_ids, oldest=oldest, selectors=selectors))
    return sessions


def resume_argv(session: Session, *, yolo: bool = False) -> list[str]:
    argv = ["claude", "--resume", session.session_id]
    if yolo:
        argv.append("--dangerously-skip-permissions")
    return argv
