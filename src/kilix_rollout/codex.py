"""Codex rollout discovery, including active and archived sessions."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re

from . import jsonl, liveness, model
from .model import Session

_UUID = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
def home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex")


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


def _message_text(value) -> str:
    if isinstance(value, str):
        return jsonl.condense(value, 500)
    if isinstance(value, list):
        pieces = []
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                candidate = item.get("text") or item.get("input_text")
                if isinstance(candidate, str):
                    pieces.append(candidate)
        return jsonl.condense(" ".join(pieces), 500)
    return ""


def _first_meta(path: str) -> dict[str, object]:
    for record in jsonl.head_records(path, 256):
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict):
            return {"timestamp": record.get("timestamp"), **payload}
    return {}


def _inspect(path: str) -> dict[str, object]:
    """Return the latest cwd, messages, and explicit turn boundary."""
    cwd = ""
    prompt = ""
    agent_message = ""
    turn_event = ""
    for record in jsonl.tail_records(path, None):
        kind = record.get("type")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if kind == "turn_context" and not cwd:
            value = payload.get("cwd")
            if isinstance(value, str) and value:
                cwd = value
        elif kind == "event_msg":
            event = payload.get("type")
            if not turn_event and event in ("task_started", "task_complete"):
                turn_event = str(event)
            if not prompt and event == "user_message":
                prompt = _message_text(payload.get("message") or payload.get("text"))
            if not agent_message and event == "agent_message":
                agent_message = _message_text(payload.get("message"))
        if cwd and prompt and agent_message and turn_event:
            break
    return {
        "cwd": cwd,
        "prompt": prompt,
        "agent_message": agent_message,
        "turn_event": turn_event,
    }


def _session_id(path: str, meta: dict[str, object]) -> str:
    raw = meta.get("id") or meta.get("session_id")
    if isinstance(raw, str) and raw:
        return raw.lower()
    matches = _UUID.findall(os.path.basename(path))
    return matches[-1].lower() if matches else ""


def _rollouts(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    try:
        return sorted(root.rglob("rollout-*.jsonl"))
    except OSError:
        return []


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
    sessions_root: str = "",
    include_archived: bool = False,
    proc_root: str = "/proc",
    since: float = 0.0,
    selectors: tuple[str, ...] = (),
) -> list[Session]:
    """Discover active rollouts and, when requested, ``archived_sessions``."""
    base = Path(root or home()).expanduser()
    active = Path(sessions_root).expanduser() if sessions_root else base / "sessions"
    roots: list[tuple[Path, bool]] = [(active, False)]
    if include_archived:
        archive = (
            base / "archived_sessions"
            if not sessions_root else active.parent / "archived_sessions"
        )
        roots.append((archive, True))

    oldest = model.cutoff(since)
    candidates: list[tuple[str, float, bool, dict[str, object]]] = []
    for directory, archived in roots:
        for path in _rollouts(directory):
            try:
                updated = path.stat().st_mtime
            except OSError:
                continue
            if oldest and updated < oldest:
                continue
            filename_ids = _UUID.findall(path.name)
            filename_id = filename_ids[-1].lower() if filename_ids else ""
            if (selectors and filename_id
                    and not _matches_selector(
                        path, session_id=filename_id, selectors=selectors)):
                # As in the retired resolver, a standard filename is enough
                # to reject an unrelated rollout without opening it.
                continue
            meta = _first_meta(str(path)) if selectors and not filename_id else {}
            session_id = filename_id or _session_id(str(path), meta)
            if selectors and not _matches_selector(
                    path, session_id=session_id, selectors=selectors):
                continue
            candidates.append((str(path), updated, archived, meta))
    candidates.sort(key=lambda item: item[1], reverse=True)

    owners = liveness.open_by(
        [path for path, _, _, _ in candidates], proc_root=proc_root)
    sessions: list[Session] = []
    for path, updated, archived, cached_meta in candidates:
        meta = cached_meta or _first_meta(path)
        session_id = _session_id(path, meta)
        display_id = session_id or Path(path).stem
        details = _inspect(path)
        pids = owners.get(path, ())
        event = str(details["turn_event"])
        state = (
            "invalid" if not session_id else
            "live" if pids else
            "cut-off" if event == "task_started" else
            "idle"
        )
        original_cwd = (
            str(meta.get("cwd")) if isinstance(meta.get("cwd"), str) else "")
        cwd = str(details["cwd"]) or original_cwd
        sessions.append(Session(
            provider="codex",
            session_id=display_id,
            path=path,
            cwd=cwd,
            original_cwd=original_cwd,
            title=str(details["prompt"]),
            updated=updated,
            state=state,
            pids=pids,
            started=_timestamp(meta.get("timestamp")),
            last_user_message=str(details["prompt"]),
            last_agent_message=str(details["agent_message"]),
            last_turn_event=event,
            version=str(meta.get("cli_version") or ""),
            entrypoint=str(meta.get("source") or ""),
            archived=archived,
            invalid_reason=(
                "no session ID was found in metadata or the filename"
                if not session_id else ""),
        ))
    return sessions


def resume_argv(session: Session, *, yolo: bool = False) -> list[str]:
    argv = ["codex"]
    if yolo:
        argv.append("--yolo")
    argv.extend(("resume", session.session_id))
    return argv
