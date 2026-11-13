"""Read-only inventory of agent recordings and Kilix's three transcript tiers.

Discovery reads bounded metadata windows, never whole conversations. Paths
remain separate entries even when two files claim the same session identity.
OMP keeps each subagent's log in its parent session's artifact directory, as
<session>/<task id>.jsonl, and subagents may spawn their own; those child logs
are entries of their own, named by their task and agent.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import unquote

PROVIDERS = ("claude", "codex", "grok", "omp", "raw")
VERSION = 2
METADATA_BYTES = 65536
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# Directory levels below each root that hold a provider's recordings. OMP
# nests subagent logs as deep as subagents spawn, so it has no fixed depth:
# only discovery's entry and time budget bounds it, and running out is reported.
DEPTHS = {"claude": 3, "codex": 3, "grok": 2, "omp": None, "raw": 1}
_DIRECTORY = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)


def label(value: object, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:limit]


def transcript_root() -> Path:
    base = Path(os.environ.get("GPU_TERMINAL_HOME") or Path.home() / ".local/gpu_terminal")
    state = Path(os.environ.get("KILIX_STATE_DIRECTORY") or base / "kilix/state")
    return Path(os.environ.get("KILIX_TRANSCRIPT_DIR") or state / "transcripts").expanduser()


def roots() -> dict[str, list[Path]]:
    home = Path.home()
    codex = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    return {
        "claude": [Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects"],
        "codex": [codex / "sessions", codex / "archived_sessions"],
        "grok": [Path(os.environ.get("GROK_HOME") or home / ".grok") / "sessions"],
        "omp": [Path(os.environ.get("PI_CODING_AGENT_DIR") or home / ".omp/agent") / "sessions"],
        "raw": [transcript_root()],
    }


def _read(path: Path, size: int = METADATA_BYTES, offset: int = 0) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        return os.pread(fd, size, offset)
    finally:
        os.close(fd)


def _rows(path: Path, size: int):
    first = _read(path)
    windows = [first]
    if size > len(first):
        start = max(len(first), size - METADATA_BYTES)
        tail = _read(path, offset=start)
        # Drop only a partial leading row. An extra preceding byte preserves
        # a complete row when the window happens to start at a boundary.
        preceding = _read(path, 1, start - 1)
        if preceding != b"\n":
            tail = tail.partition(b"\n")[2]
        windows.append(tail)
    for window in windows:
        for raw in window.split(b"\n")[:-1]:
            try:
                row = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(row, dict):
                yield row


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _codex_titles(locations: list[Path]) -> dict[str, str]:
    titles = {}
    for home in {Path(root).expanduser().parent for root in locations}:
        path = home / "session_index.jsonl"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                continue
            while source.tell() < 16 * 1024 * 1024:
                line = source.readline(METADATA_BYTES + 1)
                if not line:
                    break
                if len(line) > METADATA_BYTES:
                    break
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    title = label(row.get("thread_name"))
                    if title:
                        titles[row["id"]] = title
    return titles


def describe(path: Path | str, provider: str, *, root: Path | None = None, tier: str = "live") -> dict:
    path = Path(path).expanduser().absolute()
    provider = "omp" if provider == "qwen" else provider
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("recording must be a regular file without a symlink")
    name = path.name.removesuffix(".zst").removesuffix(".jsonl").removesuffix(".log")
    matches = _UUID.findall(name)
    session = (path.parent.name if provider == "grok" else matches[-1] if matches else
               name if provider == "raw" else "")
    title = cwd = prompt = command = parent = agent = ""
    codex_meta = spawned = False
    if provider == "raw":
        base = root or (path.parent.parent if path.parent.name in ("recent", "archive") else path.parent)
        try:
            meta = dict(line.split("=", 1) for line in _read(base / (name + ".meta")).decode(
                "utf-8", "replace").splitlines() if "=" in line)
            cwd, command = label(meta.get("cwd"), 1024), label(meta.get("cmd"))
            title = label(meta.get("title"))
        except OSError:
            pass
    elif not path.name.endswith(".zst"):
        for row in _rows(path, info.st_size):
            kind = row.get("type")
            message = row.get("message")
            if provider == "codex":
                payload = row.get("payload")
                if isinstance(payload, dict):
                    if kind == "session_meta" and not codex_meta:
                        session = label(payload.get("id"), 256) or session
                        cwd = label(payload.get("cwd"), 1024) or cwd
                        title = label(payload.get("agent_nickname")) or title
                        # A fork can copy parent headers later in the file.
                        codex_meta = bool(payload.get("id"))
                    if kind == "response_item" and payload.get("role") == "user" and not prompt:
                        prompt = label(_text(payload.get("content")))
            elif provider == "claude":
                session = label(row.get("sessionId"), 256) or session
                cwd = label(row.get("cwd"), 1024) or cwd
                if kind in ("custom-title", "ai-title", "agent-name"):
                    title = label(row.get("customTitle") or row.get("aiTitle") or row.get("agentName") or row.get("title")) or title
                if kind == "user" and isinstance(message, dict) and not row.get("isMeta") and not prompt:
                    prompt = label(_text(message.get("content")))
            elif provider == "omp":
                if kind == "session":
                    session = label(row.get("id"), 256) or session
                    cwd = label(row.get("cwd"), 1024) or cwd
                    if isinstance(row.get("parentSession"), str):
                        parent = row["parentSession"]
                if kind == "session_init" and not spawned:
                    # Written only when OMP spawns a subagent.
                    spawned, agent = True, label(row.get("agent"))
                if kind in ("title", "title_change", "session_info"):
                    title = label(row.get("title") or row.get("name")) or title
                if isinstance(message, dict) and message.get("role") == "user" and not prompt:
                    prompt = label(_text(message.get("content")))
            elif provider == "grok":
                params = row.get("params")
                if isinstance(params, dict):
                    session = label(params.get("sessionId"), 256) or session
                    update = params.get("update")
                    if isinstance(update, dict) and update.get("sessionUpdate") == "user_message_chunk" and not prompt:
                        content = update.get("content")
                        prompt = label(content.get("text")) if isinstance(content, dict) else ""
        if provider == "grok":
            cwd = unquote(path.parent.parent.name)
    if provider == "omp" and not title and not matches and (
            spawned or os.path.basename(parent) == path.parent.name + ".jsonl"):
        # A subagent's file is named by its task id, which tells siblings apart
        # better than assignments that open with the same boilerplate. A fork
        # also names a parent session but keeps a session-id file name.
        title = " · ".join(part for part in (label(name), agent) if part)
    project = Path(cwd).name if cwd else ""
    title = title or prompt or " · ".join(part for part in (project, command) if part)
    title = title or (provider + " · " + (session[:13] or name))
    key = hashlib.sha256((provider + "\0" + str(path)).encode()).hexdigest()[:24]
    return {"inventory_id": "log-" + key, "inventory_version": VERSION, "provider": provider, "session_id": session,
            "title": title, "cwd": cwd, "command": command, "project": project,
            "path": str(path), "root": str(root.absolute()) if root else "", "tier": tier,
            "updated": info.st_mtime, "size": info.st_size, "device": info.st_dev,
            "inode": info.st_ino, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _incomplete(path: Path, problem: object) -> dict:
    return {"code": "inventory_incomplete", "path": str(path), "message": str(problem)}


def raw_entries(directory: str | Path) -> list[dict]:
    root = Path(directory)
    rows = []
    for tier, folder, suffix in (("live", root, ".log"), ("recent", root / "recent", ".log.zst"),
                                 ("archived", root / "archive", ".log.zst")):
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.endswith(suffix) and entry.is_file(follow_symlinks=False):
                        try:
                            rows.append(describe(entry.path, "raw", root=root, tier=tier))
                        except (OSError, ValueError):
                            continue
        except OSError:
            continue
    return sorted(rows, key=lambda row: (-row["updated"], row["path"]))


def discover(provider: str | None = None, *, locations: dict | None = None,
             cached: dict | None = None, max_entries: int = 300000, max_seconds: float = 30) -> dict:
    provider = "omp" if provider == "qwen" else provider
    if provider is not None and provider not in PROVIDERS:
        raise ValueError("unsupported provider")
    selected = locations if locations is not None else roots()
    titles = _codex_titles(selected.get("codex", [])) if provider in (None, "codex") else {}
    rows, errors, seen = [], [], set()
    visited = 0
    deadline = time.monotonic() + max_seconds

    def walk(root: Path, depth: int | None):
        """Regular files below root, breadth first, never through a link.

        A fixed layout stops after ``depth`` directory levels. None descends as
        deep as directories nest; the shared entry and time budget is then the
        only bound, and exhausting it raises rather than passing for the end of
        the tree. Below the root each directory is opened without following a
        link and must still be the directory its parent listed. One that cannot
        be read, was replaced or was already walked is reported and skipped,
        and the rest of the tree is still walked.
        """
        nonlocal visited
        pending = deque([(root, depth, None)])
        walked = set()
        while pending:
            directory, remaining, expected = pending.popleft()
            try:
                fd = os.open(directory, _DIRECTORY | (0 if expected is None else os.O_NOFOLLOW))
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(_incomplete(directory, exc))
                continue
            try:
                info = os.fstat(fd)
                identity = (info.st_dev, info.st_ino)
                if expected is not None and identity != expected:
                    errors.append(_incomplete(directory, "directory changed during discovery"))
                    continue
                if identity in walked:
                    # Without links only a bind mount, or a filesystem without
                    # unique inodes, returns here: never list it twice or as done.
                    errors.append(_incomplete(directory, "directory reached twice during discovery"))
                    continue
                walked.add(identity)
                with os.scandir(fd) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > max_entries or time.monotonic() > deadline:
                            raise TimeoutError("inventory discovery limit reached")
                        path = directory / entry.name
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if remaining is None or remaining > 0:
                                    found = entry.stat(follow_symlinks=False)
                                    pending.append((path, None if remaining is None else remaining - 1,
                                                    (found.st_dev, found.st_ino)))
                                continue
                            regular = entry.is_file(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            errors.append(_incomplete(path, exc))
                            continue
                        if regular:
                            yield path
            except TimeoutError:
                raise
            except OSError as exc:
                errors.append(_incomplete(directory, exc))
            finally:
                os.close(fd)

    for key in PROVIDERS:
        if provider and key != provider:
            continue
        for root in selected.get(key, []):
            root = Path(root).expanduser().absolute()
            try:
                for path in walk(root, DEPTHS[key]):
                    if key == "raw":
                        if not (path.name.endswith(".log") or path.name.endswith(".log.zst")):
                            continue
                    elif key == "grok":
                        if path.name not in ("updates.jsonl", "updates.jsonl.zst"):
                            continue
                    elif not (path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")):
                        continue
                    if str(path) in seen:
                        continue
                    seen.add(str(path))
                    tier = ("recent" if key == "raw" and path.parent.name == "recent" else
                            "archived" if path.name.endswith(".zst") or root.name == "archived_sessions" else "live")
                    try:
                        old = (cached or {}).get((key, str(path)))
                        info = path.lstat()
                        if key != "raw" and old and old.get("inventory_version") == VERSION and all(old.get(field) == value for field, value in (
                                ("size", info.st_size), ("device", info.st_dev), ("inode", info.st_ino),
                                ("mtime_ns", info.st_mtime_ns), ("ctime_ns", info.st_ctime_ns))):
                            item = dict(old, root=str(root), tier=tier)
                        else:
                            item = describe(path, key, root=root, tier=tier)
                        if key == "codex" and item["session_id"] in titles:
                            item["title"] = titles[item["session_id"]]
                        rows.append(item)
                    except (OSError, ValueError) as exc:
                        errors.append({"code": "inventory_read_failed", "path": str(path), "message": str(exc)})
            except (OSError, TimeoutError) as exc:
                errors.append({"code": "inventory_incomplete", "path": str(root), "message": str(exc)})
    rows.sort(key=lambda row: (-row["updated"], row["path"]))
    return {"sessions": rows, "complete": not errors, "errors": errors}
