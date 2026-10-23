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
import time
from datetime import datetime
from urllib.parse import unquote

from . import jsonl, liveness, model
from .model import Session


def home() -> str:
    return os.environ.get("GROK_HOME") or os.path.join(os.path.expanduser("~"), ".grok")


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _alive(pid: int, proc_root: str, opened_at: object = "") -> bool:
    try:
        if os.stat(os.path.join(proc_root, str(pid))).st_uid != os.getuid():
            return False
    except OSError:
        return False
    opened = _timestamp(opened_at)
    started = liveness.start_time(pid, proc_root=proc_root) if opened else 0.0
    return not opened or (started > 0 and started <= opened + 1.0)


def active(base: str = "", *, proc_root: str = "/proc") -> dict[str, tuple[int, str]]:
    """session id -> (pid, cwd) for sessions a live process of this user holds."""
    try:
        with open(os.path.join(base or home(), "active_sessions.json"), encoding="utf-8") as handle:
            records = json.load(handle)
    except (OSError, ValueError):
        return {}
    by_pid: dict[int, tuple[float, int, str, str]] = {}
    for index, record in enumerate(records if isinstance(records, list) else []):
        if not isinstance(record, dict):
            continue
        pid, session_id = record.get("pid"), record.get("session_id")
        opened_at = record.get("opened_at")
        if isinstance(pid, int) and isinstance(session_id, str) and _alive(
                pid, proc_root, opened_at):
            candidate = (_timestamp(opened_at), index, session_id,
                         str(record.get("cwd") or ""))
            if candidate[:2] >= by_pid.get(pid, (-1.0, -1, "", ""))[:2]:
                by_pid[pid] = candidate
    return {session_id: (pid, cwd)
            for pid, (_opened, _index, session_id, cwd) in by_pid.items()}


def live_record(base: str, pid: int, *, proc_root: str = "/proc"):
    """One unambiguous registry ownership row, opened during this process instance."""
    raw = liveness.read_bounded(os.path.join(base, "active_sessions.json"), 1 << 20)
    if raw is None:
        return None
    try:
        rows = json.loads(raw.decode("utf-8"), object_pairs_hook=jsonl._unique_object)
    except (ValueError, RecursionError):
        return None
    if not isinstance(rows, list):
        return None
    matches = [r for r in rows if isinstance(r, dict) and r.get("pid") == pid]
    if len(matches) != 1:
        return None
    row = matches[0]
    sid, cwd = row.get("session_id"), row.get("cwd")
    opened = _timestamp(row.get("opened_at"))
    started = liveness.start_time(pid, proc_root=proc_root)
    if (type(row.get("pid")) is not int or pid <= 0
            or not isinstance(sid, str) or not sid or sid in {".", ".."}
            or "/" in sid or "\\" in sid or "\0" in sid
            or not isinstance(cwd, str) or not os.path.isabs(cwd)
            or not started or opened < started or opened > time.time()
            or sum(r.get("session_id") == sid for r in rows if isinstance(r, dict)) != 1):
        return None
    return sid, cwd, opened


def observed_activity(session_dir: str, *, after: float, now: float | None = None) -> str:
    """Conservative turn observation for a live pane; never skip unreadable newer data."""
    now = time.time() if now is None else now
    kinds = {"turn_started", "turn_ended", "tool_started", "tool_completed",
             "first_token", "loop_started", "goal_planner_fired", "interjected",
             "permission_requested", "permission_resolved", "permission_pending"}
    events = []
    newest_boundary = None
    newer_stamp = now
    for raw in jsonl.bounded_reverse_lines(os.path.join(session_dir, "events.jsonl")):
        record = jsonl.load(raw, strict=True) if raw is not None else None
        if (record is None or not isinstance(record.get("type"), str)
                or record["type"] not in kinds):
            return "unknown"
        stamp = _timestamp(record.get("ts"))
        if not stamp or stamp > newer_stamp or stamp < after:
            return "unknown"
        newer_stamp = stamp
        turn = record.get("turn_id")
        if "turn_id" in record and (type(turn) not in (str, int) or turn == ""):
            return "unknown"
        kind = record["type"]
        events.append(record)
        if kind in {"turn_started", "turn_ended"}:
            if newest_boundary is None:
                newest_boundary = record
            elif newest_boundary["type"] == "turn_ended":
                if kind != "turn_started":
                    return "unknown"
                left, right = newest_boundary.get("turn_id"), record.get("turn_id")
                if left is not None or right is not None:
                    if not isinstance(left, (str, int)) or type(left) is bool or left != right:
                        return "unknown"
                break
            else:
                if kind == "turn_started":
                    return "unknown"
                events.pop()
                break
    if not events or newest_boundary is None:
        return "unknown"
    if newest_boundary["type"] == "turn_ended":
        if events[-1]["type"] != "turn_started":
            return "unknown"
        # Anything after an end except another complete turn is ambiguous.
        if events[0]["type"] != "turn_ended":
            return "unknown"
        return "idle" if now - _timestamp(events[0]["ts"]) >= 5 else "working"
    turn = newest_boundary.get("turn_id")
    if any(event.get("turn_id") not in (None, turn) for event in events):
        return "unknown"
    pending = set()
    for event in reversed(events):
        kind = event["type"]
        if kind not in {"permission_requested", "permission_pending", "permission_resolved"}:
            continue
        identifiers = [event[key] for key in ("permission_id", "request_id", "call_id") if key in event]
        if any(type(value) not in (str, int) or value == "" for value in identifiers):
            return "unknown"
        if len({str(value) for value in identifiers}) > 1:
            return "unknown"
        identifier = str(identifiers[0]) if identifiers else None
        if kind != "permission_resolved":
            if kind == "permission_requested" and identifier is None and None in pending:
                return "unknown"
            pending.add(identifier)
        elif identifier in pending:
            pending.remove(identifier)
        else:
            # An unmatched resolution cannot clear somebody else's approval.
            return "unknown"
    if pending:
        return "waiting"
    latest = events[0]
    if latest["type"] in {"permission_requested", "permission_pending"}:
        return "waiting"
    if latest["type"] == "tool_started" and latest.get("tool_name") == "ask_user_question":
        return "waiting"
    return "working"


def activity(session_dir: str, *, now: float | None = None) -> str:
    """`working`, `waiting` or `idle` from the newest turn and tool events;
    `unknown` when the log says nothing yet."""
    path = os.path.join(session_dir, "events.jsonl")
    kinds = ("turn_started", "turn_ended", "tool_started", "tool_completed",
             "first_token", "loop_started", "goal_planner_fired", "interjected",
             "permission_requested", "permission_resolved", "permission_pending")
    needles = tuple(
        marker
        for kind in kinds
        for marker in (f'"type":"{kind}"'.encode(), f'"type": "{kind}"'.encode())
    )
    for record in jsonl.tail_records_matching(path, needles):
        kind = record.get("type")
        if kind == "permission_resolved":
            return "working"
        if kind in ("permission_requested", "permission_pending"):
            requested = _timestamp(record.get("ts"))
            age = (time.time() if now is None else now) - requested if requested else 1.0
            return "waiting" if age >= 1.0 else "working"
        if kind == "tool_started" and record.get("tool_name") == "ask_user_question":
            return "waiting"
        if kind in ("turn_started", "tool_started", "tool_completed", "first_token",
                    "loop_started", "goal_planner_fired", "interjected"):
            return "working"
        if kind == "turn_ended":
            ended = _timestamp(record.get("ts"))
            if ended and (time.time() if now is None else now) - ended < 5.0:
                return "working"
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
