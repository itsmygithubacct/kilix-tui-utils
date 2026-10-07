"""Codex rollout discovery, including active and archived sessions."""
from __future__ import annotations

from dataclasses import dataclass
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
_APPROVAL_RESOLVED = frozenset({
    # Rollouts do not append a separate approval-response event. An accepted
    # request is followed by its operation; a denied/aborted request is
    # eventually closed by the turn boundary.
    "exec_command_begin", "exec_command_end",
    "patch_apply_begin", "patch_apply_end",
}) | _TURN_STARTED | _TURN_COMPLETE


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


def _inspect(
    path: str,
    *,
    include_agent_message: bool = True,
) -> dict[str, object]:
    """Return the latest cwd, messages, and explicit turn boundary."""
    cwd = ""
    prompt = ""
    agent_message = ""
    turn_event = ""
    pending_tool = ""
    approval_decided = False
    for record in jsonl.tail_records_matching(
        path,
        (
            b'"turn_context"', b'"event_msg"',
            b'"exec_approval_request"', b'"apply_patch_approval_request"',
            b'"exec_command_begin"', b'"exec_command_end"',
            b'"patch_apply_begin"', b'"patch_apply_end"',
            b'"turn_aborted"', b'"task_aborted"',
            b'"role":"user"', b'"role": "user"',
            b'"role":"assistant"', b'"role": "assistant"',
        ),
        None,
    ):
        kind = record.get("type")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if kind == "turn_context" and not cwd:
            value = payload.get("cwd")
            if isinstance(value, str) and value:
                cwd = value
        elif kind == "event_msg":
            event = payload.get("type")
            if not approval_decided and event in _APPROVAL_REQUESTED:
                pending_tool = "command approval" if event == "exec_approval_request" \
                    else "file-change approval"
                approval_decided = True
            elif not approval_decided and event in _APPROVAL_RESOLVED:
                approval_decided = True
            if not turn_event and event in (_TURN_STARTED | _TURN_COMPLETE):
                turn_event = str(event)
            if not prompt and event == "user_message":
                prompt = _operator_message(
                    payload.get("message") or payload.get("text"))
            if not agent_message and event == "agent_message":
                agent_message = _message_text(payload.get("message"))
        elif kind == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if not prompt and role == "user":
                prompt = _operator_message(payload.get("content"))
            elif not agent_message and role == "assistant":
                agent_message = _message_text(payload.get("content"))
        if (cwd and prompt and turn_event
                and (agent_message or not include_agent_message)):
            break
    return {
        "cwd": cwd,
        "prompt": prompt,
        "agent_message": agent_message,
        "turn_event": turn_event,
        "pending_tool": pending_tool,
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


# ---------------------------------------------------------------------------
# Which rollout a live Codex process owns, without an open descriptor.
#
# Codex 0.160 appends to its rollout by opening and closing it per write, so
# `/proc/<pid>/fd` shows no `rollout-*.jsonl` (it holds `logs_*.sqlite` and a lock
# only). That descriptor stays the first choice (`liveness.open_by`). Without it, an
# instance (one per pane) is matched to a rollout only by evidence that cannot name
# two sessions, and anything else gives no session at all:
#
#   1. its own command line names the session: `codex resume <uuid>`;
#   2. the session's first record (`session_meta`) carries the working directory
#      and a timestamp; Codex writes it within about two seconds of starting, so a
#      rollout whose `session_meta.timestamp` falls in [start - START_BEFORE,
#      start + START_WINDOW] of exactly one live instance with that same working
#      directory, which is not a subagent thread, and whose `originator` is the
#      kind of process the instance is (`codex-tui`, or `codex_exec` for a
#      `codex exec` command line), belongs to that instance.
#
# A working directory is refused wholesale (every instance in it gets no session)
# when any rollout created since its earliest instance started cannot be attributed
# to exactly one instance by that rule: a second session started later in the same
# process (`/new`), another Codex run in that directory, two instances starting
# together. A newer session in the same process would make the first rollout's
# events stale, which is the one thing that must not be reported as a state.
# Not used: `logs_*.sqlite` (it tags log rows with `pid:<n>:<uuid>` and a thread,
# but an idle session logs no thread-tagged rows), and "the newest rollout".
START_BEFORE = 2.0
START_WINDOW = 30.0
MAX_CANDIDATES = 400
_META_CACHE: dict[str, dict | None] = {}


@dataclass(frozen=True)
class Instance:
    """One live Codex (a pane's processes): what identifies its session."""

    key: object
    start: float            # earliest process start, Unix time
    cwd: str                # the process's own working directory
    resume_id: str = ""     # `codex resume <uuid>` on its command line
    home: str = ""          # CODEX_HOME of the process, "" for the default
    pids: tuple[int, ...] = ()
    exec_run: bool = False  # `codex exec ...`: a non-interactive run, not the TUI


def originator(exec_run: bool) -> str:
    """What `session_meta.originator` says for that kind of process."""
    return "codex_exec" if exec_run else "codex-tui"


def resume_id(argv) -> str:
    """The session UUID named by `codex resume <uuid>`, or ""."""
    args = [str(value) for value in argv]
    for index, value in enumerate(args):
        if value == "resume":
            for later in args[index + 1:]:
                if later.startswith("-"):
                    continue
                match = _UUID.fullmatch(later.strip())
                return match.group(1).lower() if match else ""
    return ""


def _real(path: str) -> str:
    try:
        return os.path.realpath(path) if path else ""
    except OSError:
        return path


def _is_subagent(meta: dict) -> bool:
    return isinstance(meta.get("source"), dict) or meta.get("thread_source") == "subagent"


def _candidate_meta(path: str) -> dict | None:
    """The first record's payload with its timestamp, cached (it never changes)."""
    if path not in _META_CACHE:
        meta = _first_meta(path)
        stamp = _timestamp(meta.get("timestamp")) if meta else None
        _META_CACHE[path] = ({**meta, "_ts": stamp} if meta and stamp is not None else None)
        if len(_META_CACHE) > 4096:
            _META_CACHE.clear()
    return _META_CACHE[path]


def _day_directories(sessions: Path, earliest: float, now: float) -> list[Path]:
    """The `sessions/YYYY/MM/DD` folders from the day before `earliest` to the day after `now`."""
    found = []
    for index in range(int(earliest // 86400) - 1, int(now // 86400) + 2):
        candidate = sessions / datetime.fromtimestamp(index * 86400, timezone.utc).strftime("%Y/%m/%d")
        if candidate.is_dir():
            found.append(candidate)
    return found


def _by_id(sessions: Path, identity: str) -> str:
    """The one rollout file for a session id, or "" when there are none or several."""
    try:
        matches = [str(path) for path in sessions.glob(f"*/*/*/rollout-*{identity}.jsonl")]
    except OSError:
        return ""
    return matches[0] if len(matches) == 1 else ""


def resolve_instances(
    instances,
    *,
    now: float | None = None,
    skip: frozenset = frozenset(),
) -> dict[object, str]:
    """Map instance keys to the rollout each provably owns; the rest get no entry.

    `skip` holds paths already claimed by an open descriptor.
    """
    import time as _time

    now = _time.time() if now is None else now
    result: dict[object, str] = {}
    by_home: dict[str, list[Instance]] = {}
    for instance in instances:
        if instance.start > 0 and instance.cwd:
            by_home.setdefault(instance.home or home(), []).append(instance)
    for codex_home, group in by_home.items():
        sessions = Path(codex_home).expanduser() / "sessions"
        earliest = min(item.start for item in group)
        candidates: list[tuple[str, float, str, str]] = []
        for directory in _day_directories(sessions, earliest, now):
            try:
                names = sorted(directory.iterdir())
            except OSError:
                continue
            for path in names:
                if not path.name.startswith("rollout-") or str(path) in skip:
                    continue
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                if modified < earliest - START_BEFORE:
                    continue
                meta = _candidate_meta(str(path))
                if meta is None or _is_subagent(meta) or meta["_ts"] < earliest - START_BEFORE:
                    continue
                candidates.append((str(path), meta["_ts"], _real(str(meta.get("cwd") or "")),
                                   str(meta.get("originator") or "")))
        if len(candidates) > MAX_CANDIDATES:
            continue                                  # too many to reason about: no session
        for cwd in {_real(item.cwd) for item in group}:
            for exec_run in (False, True):
                here = [item for item in group if _real(item.cwd) == cwd and item.exec_run == exec_run]
                if not here:
                    continue
                kind = originator(exec_run)
                mine = [item for item in candidates if item[2] == cwd and item[3] == kind]
                attributed: dict[object, list[str]] = {item.key: [] for item in here}
                refused = False
                for path, stamp, _cwd, _kind in mine:
                    owners = [item for item in here
                              if item.start - START_BEFORE <= stamp <= item.start + START_WINDOW]
                    if len(owners) != 1:
                        refused = True                # nobody's, or two instances' rollout
                        break
                    attributed[owners[0].key].append(path)
                if refused:
                    continue
                for item in here:
                    if item.resume_id:
                        named = _by_id(sessions, item.resume_id)
                        if named and not attributed[item.key] and named not in skip:
                            result[item.key] = named
                    elif len(attributed[item.key]) == 1:
                        result[item.key] = attributed[item.key][0]
    return result
