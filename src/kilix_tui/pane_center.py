"""One enriched snapshot of every pane in the current Kilix window.

Kitty already knows the page tree and foreground processes. The PTY broker
knows attachment and journal state. Coding agents know which conversation a
process owns and whether its latest turn has finished. This module joins those
three views once so the interactive pane center and its CLI cannot disagree.

Refresh work is deliberately bounded by the live panes: one kitty query (made
by the caller), one broker ``list`` call, and ``/proc`` descriptor reads only
for PIDs kitty reported. It never walks a user's complete Codex history.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from kilix_rollout import claude, codex, liveness
from kilix_rollout.model import Session

from . import kitty_rc


_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
_SHELLS = frozenset({
    "ash", "bash", "dash", "fish", "ksh", "nu", "sh", "tcsh", "zsh",
})


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _one_line(value: object, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def _short_path(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


@dataclass(frozen=True)
class BrokerStatus:
    id: str
    broker_pid: int = 0
    child_pid: int = 0
    foreground_pgrp: int = 0
    started_millis: int = 0
    journal_bytes: int = 0
    journal_epoch: int = 0
    attached: bool = False
    replay_complete: bool = False
    rows: int = 0
    columns: int = 0
    cwd: str = ""
    command: str = ""

    @classmethod
    def parse(cls, record: dict[str, Any]) -> "BrokerStatus | None":
        session_id = str(record.get("id") or "")
        if not kitty_rc.valid_broker_session(session_id):
            return None
        return cls(
            id=session_id,
            broker_pid=_integer(record.get("broker_pid")),
            child_pid=_integer(record.get("child_pid")),
            foreground_pgrp=_integer(record.get("foreground_pgrp")),
            started_millis=_integer(record.get("started_millis")),
            journal_bytes=_integer(record.get("journal_bytes")),
            journal_epoch=_integer(record.get("journal_epoch")),
            attached=bool(record.get("attached")),
            replay_complete=bool(record.get("replay_complete")),
            rows=_integer(record.get("rows")),
            columns=_integer(record.get("columns")),
            cwd=str(record.get("cwd") or ""),
            command=str(record.get("command") or ""),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.id,
            "attached": self.attached,
            "replay_complete": self.replay_complete,
            "broker_pid": self.broker_pid or None,
            "child_pid": self.child_pid or None,
            "foreground_pgrp": self.foreground_pgrp or None,
            "started_millis": self.started_millis or None,
            "journal_bytes": self.journal_bytes,
            "journal_epoch": self.journal_epoch,
            "rows": self.rows or None,
            "columns": self.columns or None,
            "cwd": self.cwd or None,
            "command": self.command or None,
        }


@dataclass
class PaneInfo:
    pane: kitty_rc.Pane
    page_index: int
    activity: str = "unknown"
    doing: str = ""
    coding: Session | None = None
    broker: BrokerStatus | None = None

    @property
    def age(self) -> str:
        if self.coding is None or self.coding.updated <= 0:
            return ""
        return self.coding.age()

    @property
    def searchable(self) -> str:
        session = self.coding
        return " ".join(filter(None, (
            self.pane.title,
            self.pane.process,
            self.pane.cwd,
            self.pane.page_title,
            self.activity,
            self.doing,
            session.provider if session else "",
            session.session_id if session else "",
        )))

    def to_dict(self) -> dict[str, object]:
        pane = self.pane
        return {
            "pane_id": pane.id,
            "page": {
                "id": pane.page_id,
                "index": self.page_index,
                "title": pane.page_title,
                "os_window_id": pane.os_window_id,
            },
            "focused": pane.is_focused,
            "title": pane.title,
            "cwd": pane.cwd or None,
            "activity": self.activity,
            "doing": self.doing or None,
            "process": {
                "name": pane.process or None,
                "argv": list(pane.argv),
                "child_pid": pane.child_pid or None,
                "foreground": [
                    {
                        "pid": process.pid or None,
                        "argv": list(process.argv),
                        "cwd": process.cwd or None,
                    }
                    for process in pane.processes
                ],
            },
            "coding_session": self.coding.to_dict() if self.coding else None,
            "broker": self.broker.to_dict() if self.broker else (
                {"session_id": pane.broker_session, "available": False}
                if pane.broker_session else None
            ),
        }


@dataclass
class Snapshot:
    panes: list[PaneInfo] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)
    self_pane_id: int = 0
    broker_available: bool = False
    warnings: tuple[str, ...] = ()

    def by_id(self, pane_id: int) -> PaneInfo | None:
        return next((item for item in self.panes if item.pane.id == pane_id), None)

    def resolve(self, target: str) -> PaneInfo:
        """Resolve an ID, session prefix, exact label, or unique substring."""
        raw = target.strip()
        if not raw:
            raise ValueError("a pane target is required")
        if raw.isdigit():
            item = self.by_id(int(raw))
            if item is not None:
                return item
            raise ValueError(f"no live pane has ID {raw}")

        folded = raw.casefold()
        broker_matches = [
            item for item in self.panes
            if item.pane.broker_session.casefold().startswith(folded)
        ]
        coding_matches = [
            item for item in self.panes
            if item.coding
            and item.coding.session_id.casefold().startswith(folded)
        ]
        exact = [
            item for item in self.panes
            if folded in {
                item.pane.title.casefold(),
                item.pane.process.casefold(),
            }
        ]
        matches = broker_matches or coding_matches or exact
        if not matches:
            matches = [
                item for item in self.panes
                if folded in item.searchable.casefold()
            ]
        unique = {item.pane.id: item for item in matches}
        if len(unique) == 1:
            return next(iter(unique.values()))
        if not unique:
            raise ValueError(f"no live pane matches {target!r}")
        choices = ", ".join(
            f"{item.pane.id}:{item.pane.title or item.pane.process}"
            for item in sorted(unique.values(), key=lambda value: value.pane.id)
        )
        raise ValueError(f"pane target {target!r} is ambiguous: {choices}")

    def to_dict(self) -> dict[str, object]:
        page_ids = {item.pane.page_id for item in self.panes}
        return {
            "schema": "kilix.panes/v1",
            "generated_at": self.generated_at,
            "self_pane_id": self.self_pane_id or None,
            "counts": {"pages": len(page_ids), "panes": len(self.panes)},
            "broker_available": self.broker_available,
            "warnings": list(self.warnings),
            "panes": [item.to_dict() for item in self.panes],
        }


def _broker_statuses() -> tuple[dict[str, BrokerStatus], bool, str]:
    executable = os.environ.get("KITTY_PTY_BROKER_EXECUTABLE", "")
    runtime = os.environ.get("KITTY_PTY_BROKER_RUNTIME", "")
    if not executable or not runtime or not os.access(executable, os.X_OK):
        return {}, False, "PTY broker status is unavailable"
    try:
        done = subprocess.run(
            [executable, "--runtime-dir", runtime, "list", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
        payload = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}, False, "PTY broker did not answer"
    if not isinstance(payload, list):
        return {}, False, "PTY broker returned malformed status"
    found: dict[str, BrokerStatus] = {}
    for record in payload:
        status = BrokerStatus.parse(record) if isinstance(record, dict) else None
        if status is not None:
            found[status.id] = status
    return found, True, ""


def _process_agent(process: kitty_rc.Process) -> str:
    names = {
        (os.path.basename(value) or value).casefold()
        for value in process.argv[:2]
    }
    if "codex" in names:
        return "codex"
    if "claude" in names:
        return "claude"
    if "kimi" in names or "kimi-code" in names:
        return "kimi"
    return ""


def _argument_session(process: kitty_rc.Process) -> str:
    argv = process.argv
    for index, value in enumerate(argv):
        if value in ("--resume", "--session") and index + 1 < len(argv):
            return argv[index + 1].lower()
        for prefix in ("--resume=", "--session="):
            if value.startswith(prefix):
                return value[len(prefix):].lower()
    for value in argv:
        match = _UUID.fullmatch(value.strip("{}"))
        if match:
            return match.group(0).lower()
    return ""


def _open_files(pid: int, *, proc_root: str) -> tuple[str, ...]:
    if pid <= 0:
        return ()
    process_root = os.path.join(proc_root, str(pid))
    try:
        if os.stat(process_root).st_uid != os.getuid():
            return ()
        names = os.listdir(os.path.join(process_root, "fd"))
    except OSError:
        return ()
    found = []
    for name in names:
        try:
            target = os.readlink(os.path.join(process_root, "fd", name))
        except OSError:
            continue
        if target.endswith(" (deleted)"):
            target = target[:-10]
        if os.path.isabs(target):
            found.append(target)
    return tuple(found)


def _minimal_session(
    provider: str,
    pane: kitty_rc.Pane,
    *,
    session_id: str = "",
    status: str = "unknown",
    cwd: str = "",
    title: str = "",
    pids: tuple[int, ...] = (),
    version: str = "",
    entrypoint: str = "",
) -> Session:
    return Session(
        provider=provider,
        session_id=session_id or "unknown",
        path="",
        cwd=cwd or pane.cwd,
        original_cwd=cwd or pane.cwd,
        title=title or pane.title,
        updated=0,
        state="live",
        pids=pids or pane.pids,
        live_status=status or "unknown",
        version=version,
        entrypoint=entrypoint,
    )


def _activity(session: Session | None, pane: kitty_rc.Pane) -> str:
    if session is not None:
        status = session.live_status.strip().casefold().replace("_", "-")
        if status in {"idle", "ready"}:
            return "idle"
        if status in {"working", "active", "busy", "running"}:
            return "working"
        if status in {"waiting", "wait", "blocked"}:
            return "waiting"
        return "agent"
    process = pane.process.casefold()
    if process in _SHELLS:
        return "shell"
    if process in {"ssh", "mosh-client"}:
        return "remote"
    return "running" if process else "unknown"


def _doing(session: Session | None, pane: kitty_rc.Pane) -> str:
    if session is not None:
        if session.last_user_message:
            return _one_line(session.last_user_message)
    if pane.title and pane.title.casefold() != pane.process.casefold():
        return _one_line(pane.title)
    if session is not None and session.title:
        return _one_line(session.title)
    if pane.argv:
        return _one_line(" ".join(pane.argv))
    return pane.process


class Inspector:
    """Stateful inspector with a small mtime cache for live rollout tails."""

    def __init__(self, *, proc_root: str = "/proc") -> None:
        self.proc_root = proc_root
        self._codex_cache: dict[
            tuple[str, int, int, tuple[int, ...]], Session
        ] = {}

    def _codex_for(self, pane: kitty_rc.Pane) -> Session | None:
        owners: dict[str, set[int]] = {}
        for process in pane.processes:
            if _process_agent(process) != "codex":
                continue
            for path in _open_files(process.pid, proc_root=self.proc_root):
                name = os.path.basename(path)
                if name.startswith("rollout-") and name.endswith(".jsonl"):
                    owners.setdefault(path, set()).add(process.pid)
        if not owners:
            return None
        candidates = []
        for path, pids in owners.items():
            try:
                stat = os.stat(path)
            except OSError:
                continue
            owner_tuple = tuple(sorted(pids))
            key = (path, stat.st_mtime_ns, stat.st_size, owner_tuple)
            session = self._codex_cache.get(key)
            if session is None:
                session = codex.session_from_path(path, pids=owner_tuple)
                if session is None:
                    continue
                self._codex_cache[key] = session
            candidates.append(session)
        if len(self._codex_cache) > 128:
            keep = {
                key: value for key, value in self._codex_cache.items()
                if any(key[0] == item.path for item in candidates)
            }
            self._codex_cache = keep
        return max(candidates, key=lambda item: item.updated) if candidates else None

    def _claude_by_pid(self) -> dict[int, tuple[str, dict[str, object]]]:
        registry = os.path.join(claude.home(), "sessions")
        grouped = liveness.registry_records(
            registry, proc_root=self.proc_root)
        answer: dict[int, tuple[str, dict[str, object]]] = {}
        for session_id, records in grouped.items():
            for record in records:
                pid = _integer(record.get("pid"))
                if pid:
                    answer[pid] = (session_id, record)
        return answer

    def _coding_for(
        self,
        pane: kitty_rc.Pane,
        claude_by_pid: dict[int, tuple[str, dict[str, object]]],
    ) -> Session | None:
        if session := self._codex_for(pane):
            return session
        for process in reversed(pane.processes):
            provider = _process_agent(process)
            if provider == "claude":
                known = claude_by_pid.get(process.pid)
                session_id, record = known if known else (
                    _argument_session(process), {})
                return _minimal_session(
                    "claude", pane,
                    session_id=session_id,
                    status=str(record.get("status") or "unknown"),
                    cwd=str(record.get("cwd") or ""),
                    title=str(record.get("name") or ""),
                    pids=(process.pid,) if process.pid else (),
                    version=str(record.get("version") or ""),
                    entrypoint=str(record.get("entrypoint") or ""),
                )
            if provider == "kimi":
                return _minimal_session(
                    "kimi", pane,
                    session_id=_argument_session(process),
                    pids=(process.pid,) if process.pid else (),
                )
            if provider == "codex":
                return _minimal_session(
                    "codex", pane,
                    session_id=_argument_session(process),
                    pids=(process.pid,) if process.pid else (),
                )
        return None

    def snapshot(self, tree: kitty_rc.Tree) -> Snapshot:
        brokers, broker_available, warning = _broker_statuses()
        claude_by_pid = self._claude_by_pid()
        page_index = {
            page.id: page.index for page in tree.pages
        }
        panes = []
        for pane in tree.panes:
            coding = self._coding_for(pane, claude_by_pid)
            broker = brokers.get(pane.broker_session)
            panes.append(PaneInfo(
                pane=pane,
                page_index=page_index.get(pane.page_id, 0),
                activity=_activity(coding, pane),
                doing=_doing(coding, pane),
                coding=coding,
                broker=broker,
            ))
        warnings = (warning,) if warning else ()
        return Snapshot(
            panes=panes,
            self_pane_id=kitty_rc.self_pane_id(),
            broker_available=broker_available,
            warnings=warnings,
        )


def format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for suffix in ("B", "K", "M", "G", "T"):
        if amount < 1024 or suffix == "T":
            return f"{amount:.0f}{suffix}" if suffix == "B" else f"{amount:.1f}{suffix}"
        amount /= 1024
    return f"{value}B"


def table(snapshot: Snapshot, *, width: int = 0) -> str:
    """Compact human list; JSON remains the lossless agent interface."""
    rows = []
    for item in snapshot.panes:
        pane = item.pane
        agent = item.coding.provider if item.coding else "-"
        marker = "*" if pane.is_focused else " "
        rows.append([
            marker,
            str(pane.id),
            str(item.page_index),
            item.activity,
            agent,
            item.age or "-",
            item.doing or pane.title or pane.process or "-",
            _short_path(pane.cwd) or "-",
        ])
    headers = ["", "PANE", "PAGE", "STATE", "AGENT", "AGE", "WHAT", "CWD"]
    if not rows:
        return "(no panes)"
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    # Keep paths useful in ordinary terminals; the tail survives clipping.
    budget = width or 120
    fixed = sum(widths[:-2]) + len(widths) - 1
    widths[-2] = min(widths[-2], max(12, (budget - fixed) * 3 // 5))
    widths[-1] = min(widths[-1], max(12, budget - fixed - widths[-2]))

    def cell(value: str, index: int) -> str:
        allowance = widths[index]
        if len(value) > allowance:
            value = ("…" + value[-allowance + 1:]) if index == len(widths) - 1 \
                else (value[:allowance - 1] + "…")
        return value.ljust(allowance)

    output = [" ".join(cell(value, index) for index, value in enumerate(headers)).rstrip()]
    output.extend(
        " ".join(cell(value, index) for index, value in enumerate(row)).rstrip()
        for row in rows
    )
    return "\n".join(output)
