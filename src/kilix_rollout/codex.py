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
_TURN_STARTED = frozenset({"task_started", "turn_started"})
_TURN_ABORTED = frozenset({"turn_aborted", "task_aborted"})
# A turn ends by completing or by being aborted (Esc): the rollout records `task_started`
# then `turn_aborted` with no `task_complete`, and the next prompt starts a new turn.
_TURN_COMPLETE = frozenset({
    "task_complete", "turn_complete", "turn_completed",
}) | _TURN_ABORTED
_APPROVAL_REQUESTED = frozenset({
    "exec_approval_request", "apply_patch_approval_request",
})
# Rollouts do not append a separate approval-response event. An accepted request is followed by
# its own operation, which carries the request's call id; a denied or interrupted one is closed
# by the turn boundary.
_APPROVAL_RESOLVED_BY_ID = frozenset({
    "exec_command_begin", "exec_command_end",
    "patch_apply_begin", "patch_apply_end",
})


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


def _operator_message(value) -> str:
    """Return operator text, excluding context records injected by Codex."""
    text = _message_text(value)
    lowered = text.lstrip().casefold()
    if lowered.startswith((
        "<codex_internal_context", "<environment_context",
        "<permissions instructions>", "<skills_instructions>",
    )):
        return ""
    return text


def _first_meta(path: str) -> dict[str, object]:
    for record in jsonl.head_records(path, 256):
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict):
            return {"timestamp": record.get("timestamp"), **payload}
    return {}


MAX_TAIL_BYTES = jsonl.TAIL_BYTES      # bytes read from the end of a rollout for a state
MAX_LINE_BYTES = jsonl.LINE_BYTES      # bytes held for one record
MAX_TAIL_SECONDS = jsonl.TAIL_SECONDS
_MARKERS = (b"task_", b"turn_", b"approval", b"exec_command", b"patch_apply", b"event_msg")
_INVALID = object()


def _scalar_ids(payload: dict, *names: str):
    """The ids a payload carries under these names; `_INVALID` when one is present but not an id."""
    found = []
    for name in names:
        if name not in payload:
            continue
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return _INVALID
        if str(value):
            found.append(str(value))
    return tuple(found)


def _classify(record: dict):
    """One record as (kind, payload, event): `event` is the event type of an `event_msg`.

    Returns None for a record whose structure a state could depend on but is not valid: a `type` that
    is not a string, or an `event_msg` whose payload is not an object or whose `type` is not a string.
    """
    kind = record.get("type")
    if not isinstance(kind, str):
        return None
    payload = record.get("payload")
    if kind != "event_msg":
        return kind, payload if isinstance(payload, dict) else {}, ""
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        return None
    return kind, payload, payload["type"]


def _pending(events) -> str:
    """The approval kind still waiting after these events, oldest first; "" if none.

    A request is resolved only by a LATER command or patch event carrying its own call or approval id;
    a request without an id is never resolved by one. A repeated request after its resolution waits again.
    """
    waiting: list[tuple[tuple[str, ...], str]] = []
    for role, ids, label in events:
        if role == "request":
            waiting.append((ids, label))
        elif ids:
            waiting = [item for item in waiting if not set(ids) & set(item[0])]
    if not waiting:
        return ""
    return "command approval" if waiting[-1][1] == "exec_approval_request" else "file-change approval"


def _inspect(
    path: str,
    *,
    include_agent_message: bool = True,
) -> dict[str, object]:
    """Return the latest cwd, messages, turn boundary and pending approval, reading a bounded tail.

    The state is only as certain as the evidence read, and anything doubtful makes it `uncertain`
    (the caller reports unknown), never an older boundary:
    - an unreadable, torn or non-object line, or an `event_msg` whose payload or type is not valid,
      or an id that is not an id, newer than the newest turn boundary;
    - a line or tail beyond the byte, line or time limits before a boundary is settled;
    - a turn end whose own start is not the nearest earlier start (interleaved or unmatched turn ids),
      or is not found.
    Approvals are matched in order (`_pending`), inside the turn that is still open.
    """
    cwd = prompt = agent_message = ""
    uncertain = False
    first = True
    newest = ""                 # the newest turn boundary event
    newest_turn = ""
    settled = False             # the newest boundary is settled (a start, or an end with its own start)
    tail: list[tuple[str, tuple[str, ...], str]] = []      # approval events newer than the boundary, newest first
    for raw in jsonl.bounded_reverse_lines(
            path, max_bytes=MAX_TAIL_BYTES, max_line=MAX_LINE_BYTES, max_seconds=MAX_TAIL_SECONDS):
        if raw is None:         # a line or the rest of the tail was not read
            if not settled:
                uncertain = True
            first = False
            continue
        record = jsonl.load(raw)
        classified = _classify(record) if record is not None else None
        if classified is None:
            if not settled and (record is not None or first or any(marker in raw for marker in _MARKERS)):
                uncertain = True
            first = False
            continue
        first = False
        kind, payload, event = classified
        if kind == "turn_context" and not cwd:
            value = payload.get("cwd")
            if isinstance(value, str) and value:
                cwd = value
        elif kind == "event_msg":
            turn = _scalar_ids(payload, "turn_id")
            if turn is _INVALID:
                if not settled:
                    uncertain = True
                turn = ()
            turn_id = turn[0] if turn else ""
            if not settled:
                if event in _APPROVAL_REQUESTED and not newest:
                    ids = _scalar_ids(payload, "call_id", "approval_id")
                    if ids is _INVALID:
                        uncertain = True
                    else:
                        tail.append(("request", ids, event))
                elif event in _APPROVAL_RESOLVED_BY_ID and not newest:
                    ids = _scalar_ids(payload, "call_id")
                    if ids is _INVALID:
                        uncertain = True
                    else:
                        tail.append(("resolve", ids, event))
                elif event in _TURN_STARTED:
                    if not newest:
                        newest, newest_turn, settled = event, turn_id, True
                    elif _same_turn(newest_turn, turn_id):
                        settled = True
                    else:
                        uncertain, settled = True, True
                elif event in _TURN_COMPLETE:
                    if not newest:
                        newest, newest_turn = event, turn_id
                    elif not _same_turn(newest_turn, turn_id):
                        uncertain, settled = True, True
            if not prompt and event == "user_message":
                prompt = _operator_message(payload.get("message") or payload.get("text"))
            if not agent_message and event == "agent_message":
                agent_message = _message_text(payload.get("message"))
        elif kind == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if not prompt and role == "user":
                prompt = _operator_message(payload.get("content"))
            elif not agent_message and role == "assistant":
                agent_message = _message_text(payload.get("content"))
        if (settled and cwd and prompt
                and (agent_message or not include_agent_message)):
            break
    if newest and not settled:
        uncertain = True          # an end with no start found: nothing says which turn it ended
    pending_tool = ""
    if uncertain or not newest:
        newest, pending_tool = "", ""
    elif newest in _TURN_STARTED:
        pending_tool = _pending(reversed(tail))
    return {
        "cwd": cwd,
        "prompt": prompt,
        "agent_message": agent_message,
        "turn_event": newest,
        "pending_tool": pending_tool,
        "uncertain": uncertain,
    }


def _same_turn(left: str, right: str) -> bool:
    """Two boundary events belong to one turn: equal ids, or ids that are not both given."""
    return not left or not right or left == right


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


def _session_record(
    path: str,
    *,
    updated: float,
    archived: bool,
    pids: tuple[int, ...],
    cached_meta: dict[str, object] | None = None,
    include_agent_message: bool = True,
) -> Session:
    """Build one record without walking the rest of the Codex history."""
    meta = cached_meta or _first_meta(path)
    session_id = _session_id(path, meta)
    display_id = session_id or Path(path).stem
    details = _inspect(path, include_agent_message=include_agent_message)
    event = str(details["turn_event"])
    owners = tuple(sorted(set(int(pid) for pid in pids if int(pid) > 0)))
    state = (
        "invalid" if not session_id else
        "live" if owners else
        "cut-off" if event in _TURN_STARTED else
        "idle"
    )
    live_status = (
        "waiting" if owners and details["pending_tool"] else
        "idle" if owners and event in _TURN_COMPLETE else
        "working" if owners and event in _TURN_STARTED else
        "unknown" if owners else
        ""
    )
    original_cwd = (
        str(meta.get("cwd")) if isinstance(meta.get("cwd"), str) else "")
    cwd = str(details["cwd"]) or original_cwd
    return Session(
        provider="codex",
        session_id=display_id,
        path=path,
        cwd=cwd,
        original_cwd=original_cwd,
        title=str(details["prompt"]),
        updated=updated,
        state=state,
        pids=owners,
        live_status=live_status,
        started=_timestamp(meta.get("timestamp")),
        last_user_message=str(details["prompt"]),
        last_agent_message=str(details["agent_message"]),
        last_turn_event=event,
        pending_tool=str(details["pending_tool"]),
        version=str(meta.get("cli_version") or ""),
        entrypoint=str(meta.get("source") or ""),
        archived=archived,
        invalid_reason=(
            "no session ID was found in metadata or the filename"
            if not session_id else ""),
    )


def session_from_path(
    path: str,
    *,
    pids: tuple[int, ...] = (),
    archived: bool = False,
) -> Session | None:
    """Inspect one known rollout, for pane dashboards and targeted tooling.

    Discovery intentionally walks all recent rollouts. A live pane already
    tells us the exact file through ``/proc/<pid>/fd``; reparsing the entire
    Codex history for that case turns a refresh into needless disk work.
    """
    try:
        updated = os.stat(path).st_mtime
    except OSError:
        return None
    return _session_record(
        path,
        updated=updated,
        archived=archived,
        pids=pids,
        include_agent_message=False,
    )


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
        pids = owners.get(path, ())
        sessions.append(_session_record(
            path,
            updated=updated,
            archived=archived,
            pids=pids,
            cached_meta=cached_meta,
        ))
    return sessions


def resume_argv(session: Session, *, yolo: bool = False) -> list[str]:
    argv = ["codex"]
    if yolo:
        argv.append("--yolo")
    argv.extend(("resume", session.session_id))
    return argv

