"""The log file a live Kilix pane is actually writing.

A pane number is a Kilix window id. The program in that window decides which
file is its record:

- Codex, Claude, Grok, Kimi, and omp use that agent's own session log
- a tmux client uses the tmux-cli capture for its ``TMUX_PANE``
- anything else uses the Kilix PTY transcript for the pane's broker session

An agent whose file cannot be found is reported as missing. It is not
replaced with the Kilix transcript, because that transcript is the terminal,
not the conversation.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re

from . import claude, codex, grok, kimi, liveness, omp
from .errors import BackendError, ConflictError, NotFoundError


_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
_ROLLOUT_STAMP = re.compile(
    r"^rollout-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-"
)
# A rollout created by this process is named within a few seconds of its start.
# The shared Codex app-server keeps older sessions open beside that one.
_ROLLOUT_START_SLACK = 30.0
_BROKER = re.compile(r"^[0-9a-f]{16}$")
_TMUX_PANE = re.compile(r"^%\d+$")
_LIVE_LOG = re.compile(r"^([0-9a-f]{32})--([0-9]{6})\.log$")
_HOSTS = frozenset({"tmux", "screen", "ssh", "mosh", "mosh-client", "kitten"})
_AGENTS = {
    "claude": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "grok": "grok",
    "omp": "omp",
    "kimi": "kimi",
    "kimi-code": "kimi",
}


class PaneLog:
    """One resolved log: which kind of record it is, and where it is."""

    def __init__(self, pane_id: str, kind: str, path: str) -> None:
        self.pane_id = str(pane_id)
        self.kind = kind
        self.path = path

    def to_dict(self) -> dict[str, object]:
        pane: object = int(self.pane_id) if self.pane_id.isdecimal() else self.pane_id
        return {"pane": pane, "kind": self.kind, "path": self.path}


class Process:
    def __init__(self, argv: tuple[str, ...] | list[str], pid: int = 0, cwd: str = "") -> None:
        self.argv = tuple(str(part) for part in argv)
        self.pid = int(pid or 0)
        self.cwd = cwd or ""


class Pane:
    """The facts `log --pane` needs, split out so a test can supply them."""

    def __init__(
        self,
        pane_id: str,
        *,
        broker_session: str = "",
        cwd: str = "",
        env: dict[str, str] | None = None,
        processes: tuple[Process, ...] | list[Process] = (),
    ) -> None:
        self.pane_id = str(pane_id)
        self.broker_session = broker_session or ""
        self.cwd = cwd or ""
        self.env = dict(env or {})
        self.processes = tuple(processes)


def _basename(token: str) -> str:
    return os.path.basename(str(token).replace("\\", "/")).casefold()


def mentioned(argv: tuple[str, ...] | list[str]) -> list[str]:
    """Agents named by an argv, in order. A host program names none."""
    argv = tuple(argv)
    if not argv or _basename(argv[0]) in _HOSTS:
        return []
    found: list[str] = []
    for value in argv:
        for part in str(value).replace("\\", "/").split("/"):
            name = part.casefold()
            provider = _AGENTS.get(name)
            if provider is None:
                for known, candidate in _AGENTS.items():
                    if name.startswith((known + "-", known + ".")):
                        provider = candidate
                        break
            if provider is not None and provider not in found:
                found.append(provider)
    return found


def argument_session(argv: tuple[str, ...] | list[str]) -> str:
    """A session id passed on the command line, when the program has one."""
    argv = tuple(argv)
    for index, value in enumerate(argv):
        if value in ("--resume", "--session") and index + 1 < len(argv):
            return argv[index + 1].strip()
        for prefix in ("--resume=", "--session="):
            if value.startswith(prefix):
                return value[len(prefix):].strip()
    for value in argv:
        token = value.strip("{}")
        if _UUID.fullmatch(token):
            return token.lower()
    return ""


def _one(paths: list[str], *, kind: str, pane_id: str) -> str:
    unique = sorted(set(paths))
    if not unique:
        raise NotFoundError(
            f"pane {pane_id} is {kind} but its log file was not found")
    if len(unique) > 1:
        raise ConflictError(
            f"pane {pane_id} is {kind} and more than one log matches: "
            + ", ".join(unique))
    return unique[0]


def _open_files(pid: int, proc_root: str, accept) -> list[str]:
    directory = os.path.join(proc_root, str(pid), "fd")
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found: list[str] = []
    for name in names:
        try:
            target = os.readlink(os.path.join(directory, name))
        except OSError:
            continue
        if target.endswith(" (deleted)"):
            target = target[: -len(" (deleted)")]
        if not os.path.isabs(target) or not accept(target):
            continue
        try:
            found.append(os.path.realpath(target))
        except OSError:
            continue
    return found


def _files_named(root: str, predicate) -> list[str]:
    base = Path(root)
    if not base.is_dir():
        return []
    found: list[str] = []
    try:
        paths = base.rglob("*")
    except OSError:
        return []
    for path in paths:
        try:
            if path.is_file() and not path.is_symlink() and predicate(path):
                found.append(str(path))
        except OSError:
            continue
    return found


def _stat_fields(pid: int, proc_root: str) -> list[str] | None:
    data = liveness.read_bounded(os.path.join(proc_root, str(pid), "stat"), liveness.MAX_PROC_BYTES)
    if data is None:
        return None
    raw = data.decode("utf-8", errors="replace")
    closing = raw.rfind(")")
    if closing == -1:
        return None
    return raw[closing + 2:].split()


def _ppid(pid: int, proc_root: str) -> int | None:
    fields = _stat_fields(pid, proc_root)
    if fields is None or len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1])


def _descendants(roots: set[int], proc_root: str) -> set[int]:
    """`roots` plus every process that descends from one of them."""
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return set(roots)
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        parent = _ppid(pid, proc_root)
        if parent is not None:
            children.setdefault(parent, []).append(pid)
    found = set(roots)
    pending = list(roots)
    while pending:
        pid = pending.pop()
        for child in children.get(pid, []):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _rollout_filename_time(name: str) -> float | None:
    """Local time encoded in `rollout-YYYY-MM-DDTHH-MM-SS-...`."""
    match = _ROLLOUT_STAMP.match(name)
    if match is None:
        return None
    text = f"{match.group(1)}T{match.group(2)}:{match.group(3)}:{match.group(4)}"
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").timestamp()
    except ValueError:
        return None


def _is_rollout(path: str) -> bool:
    name = os.path.basename(path)
    return name.startswith("rollout-") and name.endswith(".jsonl")


def _codex_log(pane: Pane, processes: list[Process], *, home: str, proc_root: str) -> str:
    roots = {process.pid for process in processes if process.pid > 0}
    opened: list[str] = []
    for pid in _descendants(roots, proc_root):
        opened.extend(_open_files(pid, proc_root, _is_rollout))
    opened = sorted(set(opened))
    if len(opened) == 1:
        return opened[0]
    if len(opened) > 1:
        started = [
            liveness.start_time(process.pid, proc_root=proc_root)
            for process in processes if process.pid > 0
        ]
        started = [stamp for stamp in started if stamp > 0]
        if started:
            earliest = min(started) - _ROLLOUT_START_SLACK
            opened = [
                path for path in opened
                if (stamp := _rollout_filename_time(os.path.basename(path))) is not None
                and stamp >= earliest
            ]
        if len(opened) == 1:
            return opened[0]
        if len(opened) > 1:
            return _one(opened, kind="codex", pane_id=pane.pane_id)
    session_id = ""
    for process in processes:
        session_id = argument_session(process.argv)
        if session_id:
            break
    if not session_id:
        raise NotFoundError(
            f"pane {pane.pane_id} is codex but its rollout file was not found")
    needle = session_id.casefold()
    return _one(
        _files_named(
            os.path.join(home, "sessions"),
            lambda path: path.name.startswith("rollout-")
            and path.suffix == ".jsonl"
            and needle in path.name.casefold(),
        ),
        kind="codex", pane_id=pane.pane_id)


def _claude_from_registry(processes: list[Process], *, home: str, proc_root: str) -> str:
    records = liveness.registry_records(
        os.path.join(home, "sessions"), proc_root=proc_root, require_start=False)
    wanted = {process.pid for process in processes if process.pid > 0}
    found: list[str] = []
    for session_id, rows in records.items():
        if any(row.get("pid") in wanted for row in rows):
            found.append(session_id)
    if len(found) == 1:
        return found[0]
    return ""


def _claude_log(pane: Pane, processes: list[Process], *, home: str, proc_root: str) -> str:
    projects = os.path.join(home, "projects")
    session_id = _claude_from_registry(processes, home=home, proc_root=proc_root)
    if not session_id:
        for process in processes:
            session_id = argument_session(process.argv)
            if session_id:
                break
    if session_id:
        needle = session_id.casefold() + ".jsonl"
        return _one(
            _files_named(projects, lambda path: path.name.casefold() == needle),
            kind="claude", pane_id=pane.pane_id)
    opened: list[str] = []
    for process in processes:
        opened.extend(_open_files(
            process.pid, proc_root,
            lambda path: path.endswith(".jsonl") and "/projects/" in path.replace("\\", "/")))
    return _one(opened, kind="claude", pane_id=pane.pane_id)


def _grok_log(pane: Pane, processes: list[Process], *, home: str, proc_root: str) -> str:
    for process in processes:
        if process.pid <= 0:
            continue
        row = grok.live_record(home, process.pid, proc_root=proc_root)
        if row is None:
            continue
        session_id, cwd, _opened = row
        path = os.path.join(grok.session_dir(session_id, cwd, home), "events.jsonl")
        if os.path.isfile(path):
            return path
    for process in processes:
        session_id = argument_session(process.argv)
        if not session_id:
            continue
        cwd = process.cwd or pane.cwd
        if not cwd:
            continue
        path = os.path.join(grok.session_dir(session_id, cwd, home), "events.jsonl")
        if os.path.isfile(path):
            return path
    raise NotFoundError(f"pane {pane.pane_id} is grok but its log file was not found")


def _kimi_log(pane: Pane, processes: list[Process], *, home: str) -> str:
    session_id = ""
    for process in processes:
        session_id = argument_session(process.argv)
        if session_id:
            break
    if not session_id:
        raise NotFoundError(f"pane {pane.pane_id} is kimi but its log file was not found")
    wanted = session_id.casefold()
    directories = [
        str(record.get("sessionDir") or "")
        for record in kimi._index(home)
        if str(record.get("sessionId") or "").casefold() == wanted
    ]
    paths = [kimi.wire_path(directory) for directory in directories if directory]
    paths = [path for path in paths if os.path.isfile(path)]
    return _one(paths, kind="kimi", pane_id=pane.pane_id)


def _omp_log(pane: Pane, processes: list[Process], *, home: str, proc_root: str) -> str:
    for process in processes:
        if process.pid <= 0:
            continue
        started = liveness.start_time(process.pid, proc_root=proc_root)
        found = omp.session_for_pid(
            process.pid, root=home, proc_root=proc_root, after=started)
        if found is not None and found.path and os.path.isfile(found.path):
            return found.path
    session_id = ""
    for process in processes:
        session_id = argument_session(process.argv)
        if session_id:
            break
    if session_id:
        needle = session_id.casefold()
        return _one(
            _files_named(
                os.path.join(home, "sessions"),
                lambda path: path.suffix == ".jsonl" and needle in path.name.casefold(),
            ),
            kind="omp", pane_id=pane.pane_id)
    raise NotFoundError(f"pane {pane.pane_id} is omp but its log file was not found")


def _environ(pid: int, proc_root: str) -> dict[str, str]:
    raw = liveness.read_bounded(os.path.join(proc_root, str(pid), "environ"), 4 << 20)
    if raw is None:
        return {}
    found: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        name, separator, value = entry.partition(b"=")
        if not separator or not name:
            continue
        try:
            found[name.decode("utf-8")] = value.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return found


def _tmux_pane_id(pane: Pane, proc_root: str) -> str:
    for process in pane.processes:
        if process.pid <= 0 or _basename(process.argv[0] if process.argv else "") != "tmux":
            continue
        candidate = _environ(process.pid, proc_root).get("TMUX_PANE", "")
        if _TMUX_PANE.fullmatch(candidate):
            return candidate
    for key in ("TMUX_PANE",):
        candidate = pane.env.get(key, "")
        if _TMUX_PANE.fullmatch(candidate):
            return candidate
    return ""


def _tmux_log(pane: Pane, *, root: str, proc_root: str) -> str:
    pane_id = _tmux_pane_id(pane, proc_root)
    if not pane_id:
        raise NotFoundError(
            f"pane {pane.pane_id} is tmux but it has no TMUX_PANE to name a log")
    metadata_dir = Path(root) / "metadata"
    matches: list[tuple[str, str, dict]] = []
    if metadata_dir.is_dir():
        try:
            files = sorted(metadata_dir.glob("*.json"))
        except OSError:
            files = []
        for path in files:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("pane_id") != pane_id:
                continue
            started = str(record.get("started_utc") or "")
            matches.append((started, path.stem, record))
    if not matches:
        raise NotFoundError(
            f"pane {pane.pane_id} is tmux ({pane_id}) but no tmux log matches it")
    live = [item for item in matches if not item[2].get("ended_utc")]
    chosen = max(live or matches, key=lambda item: item[0])
    capture_id = str(chosen[2].get("capture_id") or chosen[1])
    live_dir = Path(root) / "live"
    segments: list[tuple[int, Path]] = []
    if live_dir.is_dir():
        try:
            names = list(live_dir.glob(f"{capture_id}--*.log"))
        except OSError:
            names = []
        for path in names:
            match = _LIVE_LOG.match(path.name)
            if match and match.group(1) == capture_id:
                segments.append((int(match.group(2)), path))
    if segments:
        return str(max(segments)[1])
    archive = chosen[2].get("archive_directory")
    prefix = chosen[2].get("archive_prefix")
    archived: list[Path] = []
    if isinstance(archive, str) and isinstance(prefix, str) and archive and prefix:
        directory = Path(archive)
        if directory.is_dir():
            try:
                archived = [
                    path for path in directory.glob(f"{prefix}--*.log.zst")
                    if path.is_file()
                ]
            except OSError:
                archived = []
    if archived:
        return str(max(archived))
    raise NotFoundError(
        f"pane {pane.pane_id} is tmux ({pane_id}) but its capture {capture_id} has no log file")


def transcript_dir() -> str:
    configured = os.environ.get("KITTY_PTY_BROKER_TRANSCRIPT_DIR")
    if configured:
        return configured
    state = os.environ.get("KILIX_STATE_DIRECTORY")
    if state:
        return os.path.join(state, "transcripts")
    return os.path.join(os.path.expanduser("~"), ".local/gpu_terminal/kilix/state/transcripts")


def _kilix_log(pane: Pane, *, directory: str) -> str:
    session = pane.broker_session.strip().lower()
    if not _BROKER.fullmatch(session):
        session = pane.env.get("KITTY_PTY_BROKER_SESSION", "").strip().lower()
    if not _BROKER.fullmatch(session):
        raise NotFoundError(f"pane {pane.pane_id} has no Kilix transcript session")
    root = Path(directory)
    for candidate in (
        root / f"{session}.log",
        root / "recent" / f"{session}.log.zst",
        root / "archive" / f"{session}.log.zst",
    ):
        if candidate.is_file():
            return str(candidate)
    raise NotFoundError(f"pane {pane.pane_id} has no Kilix transcript for session {session}")


def _agent_processes(pane: Pane) -> tuple[str, list[Process]] | None:
    """The innermost program that is an agent, and every process of that agent."""
    chosen = ""
    for process in reversed(pane.processes):
        names = mentioned(process.argv)
        if names:
            chosen = names[0]
            break
    if not chosen:
        return None
    return chosen, [process for process in pane.processes if chosen in mentioned(process.argv)]


def _is_tmux(pane: Pane) -> bool:
    return any(
        process.argv and _basename(process.argv[0]) == "tmux"
        for process in pane.processes
    )


def resolve(
    pane: Pane,
    *,
    proc_root: str = "/proc",
    codex_home: str = "",
    claude_home: str = "",
    grok_home: str = "",
    kimi_home: str = "",
    omp_home: str = "",
    transcript_directory: str = "",
    tmux_log_root: str = "",
) -> PaneLog:
    """The log for one pane. Raises NotFoundError or ConflictError."""
    agent = _agent_processes(pane)
    if agent is not None:
        kind, processes = agent
        if kind == "codex":
            path = _codex_log(
                pane, processes, home=codex_home or codex.home(), proc_root=proc_root)
        elif kind == "claude":
            path = _claude_log(
                pane, processes, home=claude_home or claude.home(), proc_root=proc_root)
        elif kind == "grok":
            path = _grok_log(
                pane, processes, home=grok_home or grok.home(), proc_root=proc_root)
        elif kind == "kimi":
            path = _kimi_log(pane, processes, home=kimi_home or kimi.home())
        elif kind == "omp":
            path = _omp_log(
                pane, processes, home=omp_home or omp.home(), proc_root=proc_root)
        else:
            raise NotFoundError(f"pane {pane.pane_id} has no rollout log")
        return PaneLog(pane.pane_id, kind, path)
    if _is_tmux(pane):
        root = tmux_log_root or os.path.join(
            os.path.expanduser("~"), ".gpu_terminal", "tmux-cli", "logs")
        return PaneLog(pane.pane_id, "tmux", _tmux_log(pane, root=root, proc_root=proc_root))
    directory = transcript_directory or transcript_dir()
    return PaneLog(pane.pane_id, "kilix", _kilix_log(pane, directory=directory))


def pane_from_window(window: dict, pane_id: str) -> Pane:
    """A Pane from one ``kitten @ ls`` window object."""
    env = window.get("env") if isinstance(window.get("env"), dict) else {}
    processes: list[Process] = []
    for raw in window.get("foreground_processes") or []:
        if not isinstance(raw, dict):
            continue
        argv = raw.get("cmdline") or []
        if not isinstance(argv, list):
            continue
        try:
            pid = int(raw.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        processes.append(Process(argv, pid=pid, cwd=str(raw.get("cwd") or "")))
    shell = window.get("cmdline") or []
    try:
        shell_pid = int(window.get("pid") or 0)
    except (TypeError, ValueError):
        shell_pid = 0
    if isinstance(shell, list) and shell and all(process.pid != shell_pid for process in processes):
        processes.append(Process(shell, pid=shell_pid, cwd=str(window.get("cwd") or "")))
    broker = str(env.get("KITTY_PTY_BROKER_SESSION") or "")
    return Pane(
        pane_id,
        broker_session=broker,
        cwd=str(window.get("cwd") or ""),
        env={str(key): str(value) for key, value in env.items()},
        processes=processes,
    )


def _windows(node: object):
    if isinstance(node, dict):
        if "id" in node and ("cmdline" in node or "foreground_processes" in node):
            yield node
        for value in node.values():
            yield from _windows(value)
    elif isinstance(node, list):
        for item in node:
            yield from _windows(item)


def load_live_pane(pane_id: str, *, kilix: str = "kilix", timeout: float = 10.0) -> Pane:
    """Read one live Kilix pane from ``kilix @ ls``."""
    import subprocess
    try:
        result = subprocess.run(
            [kilix, "@", "ls", "--match", f"id:{pane_id}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackendError(f"could not list Kilix panes: {error}") from None
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise BackendError(f"could not list Kilix panes: {detail}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as error:
        raise BackendError(f"Kilix pane list was not JSON: {error}") from None
    for window in _windows(payload):
        if str(window.get("id")) == str(pane_id):
            return pane_from_window(window, str(pane_id))
    raise NotFoundError(f"no Kilix pane with id {pane_id}")
