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


# What a rollout is known to hold. A record newer than the newest turn boundary that is none of these is
# not understood, so the state is unknown rather than the older boundary.
_KNOWN_KINDS = frozenset({
    "session_meta", "event_msg", "response_item", "turn_context", "compacted", "world_state",
    "token_usage_record", "inter_agent_communication_metadata",
})
_NEUTRAL_EVENTS = frozenset({
    "user_message", "agent_message", "agent_message_delta", "agent_message_content_delta",
    "agent_reasoning", "agent_reasoning_delta", "agent_reasoning_raw_content",
    "agent_reasoning_raw_content_delta", "agent_reasoning_section_break", "reasoning_content_delta",
    "token_count", "item_started", "item_completed", "thread_settings_applied", "context_compacted",
    "thread_rolled_back", "turn_diff", "plan_update", "plan_delta", "background_event", "stream_error",
    "error", "warning", "web_search_begin", "web_search_end", "mcp_tool_call_begin", "mcp_tool_call_end",
    "exec_command_output_delta", "terminal_interaction", "view_image_tool_call", "entered_review_mode",
    "exited_review_mode", "session_configured", "shutdown_complete", "undo_started", "undo_completed",
    "patch_apply_updated", "dynamic_tool_call_request", "dynamic_tool_call_response",
    "collab_agent_spawn_begin", "collab_agent_spawn_end", "collab_agent_interaction_begin",
    "collab_agent_interaction_end", "collab_waiting_begin", "collab_waiting_end", "collab_close_begin",
    "collab_close_end", "collab_resume_begin", "collab_resume_end", "mcp_startup_update",
    "mcp_startup_complete", "deprecation_notice", "raw_response_item",
})
_NEUTRAL_RESPONSE = frozenset({
    "message", "reasoning", "function_call", "function_call_output", "custom_tool_call",
    "custom_tool_call_output", "local_shell_call", "web_search_call", "agent_message",
    "ghost_snapshot", "compaction", "tool_search_call", "tool_search_output", "image_generation_call",
})


def _strict(record) -> tuple[str, dict, str] | None:
    """(kind, payload, event) of a record newer than the turn boundary, or None if it is not understood.

    Not understood: not an object, a `type` that is not a string or is unknown, a payload that is not
    an object, an `event_msg` or `response_item` of an unknown type.
    """
    if not isinstance(record, dict):
        return None
    kind = record.get("type")
    payload = record.get("payload")
    if not isinstance(kind, str) or kind not in _KNOWN_KINDS or not isinstance(payload, dict):
        return None
    if kind == "event_msg":
        event = payload.get("type")
        known = (_TURN_STARTED | _TURN_COMPLETE | _APPROVAL_REQUESTED | _APPROVAL_RESOLVED_BY_ID
                 | _NEUTRAL_EVENTS)
        if not isinstance(event, str) or event not in known:
            return None
        return kind, payload, event
    if kind == "response_item":
        inner = payload.get("type")
        if not isinstance(inner, str) or inner not in _NEUTRAL_RESPONSE:
            return None
    return kind, payload, ""


def _lenient(record: dict) -> tuple[str, dict, str]:
    """The same split for an older record, where only turn boundaries and display fields matter."""
    kind = record.get("type")
    payload = record.get("payload")
    kind = kind if isinstance(kind, str) else ""
    payload = payload if isinstance(payload, dict) else {}
    event = payload.get("type") if kind == "event_msg" else ""
    return kind, payload, event if isinstance(event, str) else ""


def _pending(events, open_turn: str):
    """(approval label still waiting or "", uncertain) after these events, oldest first.

    A request is resolved only by a LATER command or patch event that shares one of its call or
    approval ids; ids are compared on both sides. A resolution whose turn id differs from its request's,
    or an approval of another turn than the open one, cannot be attributed: uncertain.
    """
    waiting: list[tuple[tuple[str, ...], str, str]] = []
    for role, ids, label, turn in events:
        if turn and open_turn and turn != open_turn:
            return "", True
        if role == "request":
            waiting.append((ids, label, turn))
            continue
        kept = []
        for item in waiting:
            if set(ids) & set(item[0]):
                if turn and item[2] and turn != item[2]:
                    return "", True
                continue
            kept.append(item)
        waiting = kept
    if not waiting:
        return "", False
    return ("command approval" if waiting[-1][1] == "exec_approval_request" else "file-change approval"), False


def _inspect(
    path: str,
    *,
    include_agent_message: bool = True,
) -> dict[str, object]:
    """Return the latest cwd, messages, turn boundary and pending approval, reading a bounded tail.

    The state is only as certain as the evidence read, and anything doubtful makes it `uncertain`
    (the caller reports unknown), never an older boundary:
    - any record newer than the newest turn boundary that is unreadable, not an object, or not
      understood (`_strict`), or carries an id that is not an id;
    - a line or tail beyond the byte, line or time limits before the boundary is settled;
    - a turn end whose own start is not the nearest earlier start (interleaved or unmatched turn ids), or
      that is not found; a start whose previous boundary is another start (overlapping open turns);
    - approvals of another turn than the open one, or resolved by another turn's event.
    Approvals are matched in order and by id (`_pending`), inside the turn that is still open.
    """
    cwd = prompt = agent_message = ""
    uncertain = False
    newest = ""                 # the newest turn boundary event
    first_boundary = ""         # the same, believed without any check: for the recovery tool's cut-off flag
    newest_turn = ""
    settled = False             # the newest boundary is settled (a start, or an end with its own start)
    need_previous = False       # a settled turn start is still waiting for its previous boundary
    tail: list[tuple[str, tuple[str, ...], str, str]] = []   # approval events newer than the boundary, newest first
    for raw in jsonl.bounded_reverse_lines(
            path, max_bytes=MAX_TAIL_BYTES, max_line=MAX_LINE_BYTES, max_seconds=MAX_TAIL_SECONDS):
        if raw is None:         # a line or the rest of the tail was not read
            if not settled or need_previous:
                uncertain = True
            need_previous = False
            if settled:
                break
            continue
        record = jsonl.load(raw, strict=not settled or need_previous)
        if not settled:
            classified = _strict(record)
            if classified is None:
                uncertain = True
                continue
        elif record is None:
            if need_previous and any(marker in raw for marker in _MARKERS):
                uncertain, need_previous = True, False
            continue
        else:
            classified = _lenient(record)
        kind, payload, event = classified
        if kind == "turn_context" and not cwd:
            value = payload.get("cwd")
            if isinstance(value, str) and value:
                cwd = value
        elif kind == "event_msg":
            if not first_boundary and event in (_TURN_STARTED | _TURN_COMPLETE):
                first_boundary = event
            turn = _scalar_ids(payload, "turn_id")
            turn_id = turn[0] if turn and turn is not _INVALID else ""
            if turn is _INVALID and not settled:
                uncertain = True
            if need_previous:
                if event in _TURN_STARTED:
                    uncertain, need_previous = True, False        # overlapping open turns
                elif event in _TURN_COMPLETE:
                    need_previous = False
            elif not settled:
                if event in _APPROVAL_REQUESTED and not newest:
                    ids = _scalar_ids(payload, "call_id", "approval_id")
                    if ids is _INVALID:
                        uncertain = True
                    else:
                        tail.append(("request", ids, event, turn_id))
                elif event in _APPROVAL_RESOLVED_BY_ID and not newest:
                    ids = _scalar_ids(payload, "call_id", "approval_id")
                    if ids is _INVALID:
                        uncertain = True
                    else:
                        tail.append(("resolve", ids, event, turn_id))
                elif event in _TURN_STARTED:
                    if not newest:
                        newest, newest_turn, settled, need_previous = event, turn_id, True, True
                    elif _same_turn(newest_turn, turn_id):
                        settled, need_previous = True, True
                    else:
                        uncertain, settled = True, True
                elif event in _TURN_COMPLETE:
                    if not newest:
                        newest, newest_turn = event, turn_id
                    else:           # a turn ends once: a second end before any start is not a coherent history
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
        if (settled and not need_previous and cwd and prompt
                and (agent_message or not include_agent_message)):
            break
    if newest and not settled:
        uncertain = True          # an end with no start found: nothing says which turn it ended
    pending_tool = ""
    if not uncertain and newest in _TURN_STARTED:
        pending_tool, doubtful = _pending(reversed(tail), newest_turn)
        uncertain = doubtful
    if uncertain or not newest:
        newest, pending_tool = "", ""
    return {
        "cwd": cwd,
        "prompt": prompt,
        "agent_message": agent_message,
        "turn_event": newest,
        "boundary": first_boundary,
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
        "cut-off" if str(details["boundary"]) in _TURN_STARTED else
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

