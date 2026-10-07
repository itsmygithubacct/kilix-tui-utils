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

from kilix_rollout import claude, liveness
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
    if "grok" in names:
        return "grok"
    if "omp" in names:
        return "omp"
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


def _live_argv(pid: int, *, proc_root: str) -> tuple[str, ...] | None:
    """The command line the process has NOW, from /proc/<pid>/cmdline; None if unreadable."""
    if pid <= 0:
        return None
    try:
        with open(os.path.join(proc_root, str(pid), "cmdline"), "rb") as handle:
            raw = handle.read(65536)
    except OSError:
        return None
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return tuple(part.decode("utf-8", errors="surrogateescape") for part in parts) or None


def _live_environment(pid: int, *, proc_root: str) -> dict[str, str] | None:
    """The environment the process started with; None if it cannot be read."""
    try:
        with open(os.path.join(proc_root, str(pid), "environ"), "rb") as handle:
            raw = handle.read(1 << 20)
    except OSError:
        return None
    found: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        name, separator, value = entry.partition(b"=")
        if separator and name:
            found[name.decode("utf-8", errors="surrogateescape")] = value.decode("utf-8", errors="surrogateescape")
    return found


def _agent_names(argv) -> set[str]:
    return {(os.path.basename(value) or value).casefold() for value in tuple(argv)[:2]}


# The executable forms in which a process is accepted AS Claude Code for a state:
#   claude ...                                    the launcher or native binary, argv[0] basename `claude`
#   /path/to/claude ...                           the same by path
#   node|nodejs|bun /path/to/claude ...           a JavaScript runtime whose script is a file named `claude`
#   node|nodejs|bun .../@anthropic-ai/claude-code/cli.js|cli.mjs ...   the npm entrypoint
# Never by an argument that merely mentions claude (`less claude`, `grep claude`).
_JS_RUNTIMES = frozenset({"node", "nodejs", "bun"})


def _claude_form(argv) -> bool:
    argv = tuple(argv)
    if not argv:
        return False
    if os.path.basename(argv[0]) == "claude":
        return True
    if os.path.basename(argv[0]) in _JS_RUNTIMES and len(argv) > 1 and not argv[1].startswith("-"):
        script = argv[1]
        if os.path.basename(script) == "claude":
            return True
        parts = script.split("/")
        return (os.path.basename(script) in ("cli.js", "cli.mjs")
                and len(parts) >= 3 and parts[-2] == "claude-code" and parts[-3] == "@anthropic-ai")
    return False


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
        # Only Claude Code is certified: its registry names the process instance and its state.
        # Every other provider (Codex, Grok, OMP, Kimi, anything unknown) has no exact, current
        # evidence of the session a process runs now, so it is `agent`, never idle/working/waiting.
        if session.provider != "claude":
            return "agent"
        # Claude's registry has its own vocabulary (`shell` is idle with background work
        # running); only what it defines is mapped.
        return claude.ACTIVITY.get(session.live_status.strip().casefold().replace("_", "-"), "agent")
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
        self._registries: dict[str, dict[str, tuple[dict[str, object], ...]]] = {}

    def _claude_registry(self, config: str) -> dict[str, tuple[dict[str, object], ...]]:
        if config not in self._registries:
            self._registries[config] = liveness.registry_records(
                os.path.join(config, "sessions"), proc_root=self.proc_root, require_start=True)
        return self._registries[config]

    def _instance(self, pid: int):
        """(start time, command line, config directory) of the process NOW; None if any is unknown."""
        start = liveness.start_ticks(pid, proc_root=self.proc_root)
        argv = _live_argv(pid, proc_root=self.proc_root)
        environment = _live_environment(pid, proc_root=self.proc_root)
        if start is None or argv is None or environment is None:
            return None
        config = environment.get("CLAUDE_CONFIG_DIR") or (
            os.path.join(environment["HOME"], ".claude") if environment.get("HOME") else "")
        if not config or not os.path.isabs(config):
            return None
        return start, argv, config

    def _claude_by_pid(self, processes) -> dict[int, tuple[str, dict[str, object]]]:
        """pid -> (session id, registry record) for the Claude processes that a record names exactly.

        A process counts only if it is recognised as Claude by its executable form (`_claude_form`),
        its whole live command line equals the pane's, and a registry row of its own config directory
        (`CLAUDE_CONFIG_DIR`, else `$HOME/.claude`, from its environment) names its pid with a
        `procStart` equal to its start time. The process is read again when the row is USED (rows are
        cached per snapshot) and must be unchanged: same start, command line and config directory.
        A pid with more than one row, or any change meanwhile, has none.
        """
        found: dict[int, tuple[str, dict[str, object]]] = {}
        for process in processes:
            snapshot = tuple(process.argv)
            if not _claude_form(snapshot):
                continue
            before = self._instance(process.pid)
            if before is None or before[1] != snapshot or not _claude_form(before[1]):
                continue
            matches = [
                (session_id, record)
                for session_id, records in self._claude_registry(before[2]).items()
                for record in records if _integer(record.get("pid")) == process.pid
            ]
            if len(matches) != 1:
                continue
            after = self._instance(process.pid)
            if after != before or str(matches[0][1].get("procStart")) != before[0]:
                continue
            found[process.pid] = matches[0]
        return found

    def _coding_for(
        self,
        pane: kitty_rc.Pane,
        claude_by_pid: dict[int, tuple[str, dict[str, object]]],
    ) -> Session | None:
        agents = [(process, _process_agent(process)) for process in pane.processes]
        agents = [(process, provider) for process, provider in agents if provider]
        if not agents:
            return None
        process, provider = agents[0]
        pids = tuple(p.pid for p, _ in agents if p.pid > 0)
        kinds = {kind for _, kind in agents}
        if len(kinds) > 1 or sum(kind == "claude" for _, kind in agents) > 1:
            # Which agent the pane's state should describe cannot be told, whatever the order of its
            # processes (Claude beside Codex, Kimi, Grok, OMP; two Claude): no state.
            return _minimal_session(provider, pane, session_id=_argument_session(process), pids=pids)
        if provider == "claude":
            known = claude_by_pid.get(process.pid)
            session_id, record = known if known else (_argument_session(process), {})
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
        # Codex, Grok, OMP, Kimi: shown (command, directory, title, an id the command line names), but
        # with no state: their records do not name the session a process runs now.
        return _minimal_session(provider, pane, session_id=_argument_session(process), pids=pids)

    def snapshot(self, tree: kitty_rc.Tree) -> Snapshot:
        brokers, broker_available, warning = _broker_statuses()
        self._registries = {}
        claude_by_pid = self._claude_by_pid([
            process for pane in tree.panes for process in pane.processes
            if _process_agent(process) == "claude"])
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
