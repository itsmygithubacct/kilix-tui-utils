"""The catalog of VM definitions, and the one small favorites/recents record.

Definitions are configuration: one JSON file per VM, written only by an
explicit import, accept, or remove. Favorites and recents are state, kept the
way ``kilix_desk/durable.py`` keeps the desktop's record: one small file, an
atomic replace, write-on-change only, and a damaged or unwritable file that
degrades to "nothing remembered" rather than a crash.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone

from . import model
from .model import DefinitionError, VmDefinition

MAX_RECENTS = 5
# A deeply nested file makes json raise RecursionError, which is not a
# ValueError; it is damage like any other.
DAMAGE = (OSError, ValueError, RecursionError)
# The TUI writes state from its main thread (f) and from its worker thread
# (a launch records a recent); every read-modify-write holds this lock.
_STATE_LOCK = threading.RLock()
# ...and, across processes (the CLI beside a running TUI), an flock on a
# lock file next to the state file. Taken once per outermost holder.
_flock = {"depth": 0, "fd": -1}
# An update holds the flock for milliseconds. A peer that holds it longer
# was stopped (Ctrl-Z) or hung; waiting for it would freeze the picker.
LOCK_WAIT_SECONDS = 5.0
# A swap file is in flight for milliseconds. One older than this was left by
# a writer that crashed between mkstemp and replace.
STALE_SWAP_SECONDS = 600.0
_SWAP_NAME = re.compile(r"\.(?P<target>[^/]+\.json)\.[a-z0-9_]{8}\.tmp\Z")


# ── definitions ──────────────────────────────────────────────────────────────


def _definition_path(vm_id: str) -> str:
    if not model.ID_PATTERN.fullmatch(vm_id):
        raise DefinitionError(f"{vm_id!r} is not a valid VM id")
    return os.path.join(model.vms_dir(), f"{vm_id}.json")


def _open_regular(path: str):
    """Open `path` for reading as UTF-8 text, only if it is a regular file.

    O_NONBLOCK keeps a FIFO (or a device) planted under a definition's or
    the state file's name from blocking the open; it is then refused as not
    a regular file, like any other damage, instead of hanging every reader.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{os.path.basename(path)} is not a regular file")
        os.set_blocking(fd, True)
        return os.fdopen(fd, encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def load_all() -> tuple[list[VmDefinition], list[str]]:
    """Every valid definition, plus one line per file that did not load."""
    directory = model.vms_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return [], []
    found: list[VmDefinition] = []
    problems: list[str] = []
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        path = os.path.join(directory, name)
        try:
            with _open_regular(path) as handle:
                vm = VmDefinition.from_json(json.load(handle))
        except RecursionError:
            problems.append(f"{name}: nested too deeply to read")
            continue
        except (OSError, ValueError) as error:
            problems.append(f"{name}: {error}")
            continue
        except Exception as error:      # damage in one file stays in it
            problems.append(f"{name}: cannot be read "
                            f"({type(error).__name__}: {error})")
            continue
        if f"{vm.id}.json" != name:
            problems.append(f"{name}: id {vm.id!r} does not match the file")
            continue
        try:
            opened_paths(vm)            # every path must be one os can take
        except Exception as error:
            problems.append(f"{name}: a path cannot be used "
                            f"({type(error).__name__}: {error})")
            continue
        found.append(vm)
    clean: list[VmDefinition] = []
    for vm, reason in zip(found, control_conflicts(found)):
        if reason:
            problems.append(
                f"{vm.id}.json: not loaded: {reason}. Edit or delete "
                f"{_definition_path(vm.id)}, then import the VM again")
            continue
        clean.append(vm)
    return clean, problems


def opened_paths(vm: VmDefinition) -> set[str]:
    """Every pidfile and unix socket `vm` opens or watches, canonically."""
    paths = model.control_paths(vm.argv, vm.cwd or "/")
    paths |= {vm.pidfile, vm.qmp}
    return {model.canonical(path) for path in paths}


def control_conflicts(machines: list[VmDefinition]) -> list[str]:
    """For each definition, why it may not be loaded ('' when it may).

    Each VM's status and controls go through its pidfile and QMP socket, so
    two definitions that open one of them would read and drive one QEMU.
    Who keeps such a path never depends on the order the files load in:

    * in the runtime directory the manager names files after VM ids
      (``<id>.pid``, ``<id>.qmp``), so only the VM with that id may open
      them. Any other definition that does is refused, whether or not a VM
      with that id is catalogued;
    * elsewhere a path has no derived owner. When it is some definition's
      pidfile or QMP socket and more than one definition opens it, every
      one of them is refused, because nothing says which is the original.
    """
    opened = [opened_paths(vm) for vm in machines]
    managed: set[str] = set()
    for vm in machines:
        managed |= {model.canonical(vm.pidfile), model.canonical(vm.qmp)}
    openers: dict[str, list[str]] = {}
    for vm, paths in zip(machines, opened):
        for path in paths:
            openers.setdefault(path, []).append(vm.id)
    reasons: list[str] = []
    for vm, paths in zip(machines, opened):
        own = {model.canonical(model.pid_path(vm.id)),
               model.canonical(model.qmp_path(vm.id))}
        found: list[str] = []
        for path in sorted(paths):
            if model.runtime_dir_alias(path):
                if path in own:
                    continue
                stem, kind = os.path.splitext(os.path.basename(path))
                what = {".pid": "pidfile", ".qmp": "QMP socket"}.get(kind)
                found.append(f"{path}, the manager's {what} for VM {stem}"
                             if what else
                             f"{path} in the manager's runtime directory")
            elif path in managed and len(openers[path]) > 1:
                others = sorted(set(openers[path]) - {vm.id})
                found.append(f"{path}, which {', '.join(others)} "
                             f"open{'s' if len(others) == 1 else ''} too")
        if not found:
            reasons.append("")
        elif all(", which " not in item for item in found):
            reasons.append(f"it opens {' and '.join(found)}, not its own")
        else:
            reasons.append(
                f"it opens {' and '.join(found)}; a pidfile or QMP socket "
                "belongs to one VM, and outside the runtime directory the "
                "manager cannot tell which, so none of them is loaded")
    return reasons


def claimed_paths(machines: list[VmDefinition]) -> dict[str, str]:
    """Each catalogued VM's pidfile and QMP socket path, mapped to its id."""
    owners: dict[str, str] = {}
    for vm in machines:
        owners[model.canonical(vm.pidfile)] = vm.id
        owners[model.canonical(vm.qmp)] = vm.id
    return owners


def get(vm_id: str) -> VmDefinition | None:
    try:
        with _open_regular(_definition_path(vm_id)) as handle:
            return VmDefinition.from_json(json.load(handle))
    except DAMAGE:
        return None


def find(value: str) -> VmDefinition | None:
    """Look a VM up by id or, case-insensitively, by display name."""
    wanted = value.strip()
    machines, _problems = load_all()
    for vm in machines:
        if vm.id == wanted:
            return vm
    for vm in machines:
        if vm.name.casefold() == wanted.casefold():
            return vm
    return None


class StateBusy(OSError):
    """Another process has held the state lock for over LOCK_WAIT_SECONDS."""


def _take_flock(fd: int, path: str) -> bool:
    """Lock `fd` within LOCK_WAIT_SECONDS. False when flock cannot be used
    here at all; StateBusy when a peer holds it for longer."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise StateBusy(
                    f"another process (normally another kilix-qemu-"
                    f"manager) has held {path} for over "
                    f"{LOCK_WAIT_SECONDS:g}s") from None
            time.sleep(0.02)
        except OSError:
            return False


@contextlib.contextmanager
def state_lock():
    """Serialise a read-modify-write of the state file, in and across
    processes. A lock file that cannot be opened degrades to the thread
    lock alone; it never stops the manager. A peer that holds the lock for
    over LOCK_WAIT_SECONDS raises StateBusy, an OSError, rather than freeze
    the caller: that update is not made, and strict callers say so."""
    with _STATE_LOCK:
        if _flock["depth"] == 0:
            path = model.state_path() + ".lock"
            try:
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
                             | os.O_NOFOLLOW, 0o600)
            except OSError:
                fd = -1
            if fd >= 0:
                try:
                    taken = _take_flock(fd, path)
                except BaseException:   # StateBusy, or an interrupt
                    os.close(fd)
                    raise
                if not taken:
                    os.close(fd)
                    fd = -1
            _flock["fd"] = fd
        _flock["depth"] += 1
        try:
            yield
        finally:
            _flock["depth"] -= 1
            if _flock["depth"] == 0 and _flock["fd"] >= 0:
                os.close(_flock["fd"])      # closing releases the flock
                _flock["fd"] = -1


def _swap_target_ok(directory: str):
    """Which swap targets `directory` may hold, as a test on the name.

    Only the manager's own two directories are cleaned, and only for files
    it writes there: ``<id>.json`` in the vms directory, ``state.json`` in
    the state directory. KILIX_QEMU_HOME may be any directory, and the state
    file then sits directly in it, beside files that are not the manager's.
    """
    here = os.path.abspath(directory)
    if here == os.path.abspath(model.vms_dir()):
        return lambda target: bool(model.ID_PATTERN.fullmatch(
            target[: -len(".json")]))
    if here == os.path.abspath(os.path.dirname(model.state_path())):
        return lambda target: target == os.path.basename(model.state_path())
    return None


def clean_stale_swaps(directory: str, *, now: float | None = None) -> list[str]:
    """Remove swap files a crashed writer left behind; return their names.

    Only in the manager's own vms and state directories, and only names
    this module's mkstemp makes for a file it writes there
    (``.<id>.json.XXXXXXXX.tmp``, ``.state.json.XXXXXXXX.tmp``); only
    regular files (never a symlink) owned by this user and older than
    STALE_SWAP_SECONDS, so a write in progress in another process keeps its
    swap file.
    """
    moment = time.time() if now is None else now
    removed: list[str] = []
    target_ok = _swap_target_ok(directory)
    if target_ok is None:
        return removed
    try:
        names = os.listdir(directory)
    except OSError:
        return removed
    for name in names:
        match = _SWAP_NAME.fullmatch(name)
        if not match or not target_ok(match.group("target")):
            continue
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or moment - info.st_mtime < STALE_SWAP_SECONDS):
                continue
            os.unlink(path)
        except OSError:
            continue
        removed.append(name)
    return removed


def _write_json(path: str, data: object) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    clean_stale_swaps(directory)
    # A unique swap file per write, so two writers never share one.
    fd, swap = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.",
                                suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(swap, path)
    finally:
        if os.path.exists(swap):
            os.unlink(swap)


def save(vm: VmDefinition, *, replace: bool = False) -> str:
    VmDefinition.from_json(vm.as_json())       # never write what cannot load
    path = _definition_path(vm.id)
    if os.path.exists(path) and not replace:
        raise DefinitionError(f"a VM with id {vm.id!r} already exists")
    _write_json(path, vm.as_json())
    return path


def remove(vm_id: str) -> str:
    """Delete one definition file. Disks and every other file stay."""
    path = _definition_path(vm_id)
    os.unlink(path)
    try:
        with state_lock():
            record = load_state()
            record["favorites"] = [item for item in record["favorites"]
                                   if item != vm_id]
            record["recents"] = [row for row in record["recents"]
                                 if row["id"] != vm_id]
            save_state(record)
    except OSError:
        pass        # a mark for an id with no definition is never shown
    return path


# ── favorites and recents ────────────────────────────────────────────────────


def _ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if (isinstance(item, str) and model.ID_PATTERN.fullmatch(item)
                and item not in out):
            out.append(item)
    return out


def _recents(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        vm_id, when = row.get("id"), row.get("at")
        if (isinstance(vm_id, str) and model.ID_PATTERN.fullmatch(vm_id)
                and isinstance(when, str)
                and all(item["id"] != vm_id for item in out)):
            out.append({"id": vm_id, "at": when})
    return out[:MAX_RECENTS]


def load_state() -> dict:
    """The record, whole and valid; a missing or damaged file is empty."""
    try:
        with _open_regular(model.state_path()) as handle:
            record = json.load(handle)
    except DAMAGE:
        record = {}
    if not isinstance(record, dict):
        record = {}
    return {"favorites": _ids(record.get("favorites")),
            "recents": _recents(record.get("recents"))}


def save_state(record: dict, *, strict: bool = False) -> bool:
    """Write atomically, and only when the record changed. True if written.

    An unwritable state file returns False (a manager that cannot persist
    still runs), unless ``strict``: then the OSError reaches the caller, so a
    user who asked for a change can be told it was not saved.
    """
    clean = {"favorites": _ids(record.get("favorites")),
             "recents": _recents(record.get("recents"))}
    try:
        with state_lock():
            if clean == load_state():
                return False
            _write_json(model.state_path(), clean)
    except OSError:                     # StateBusy included
        if strict:
            raise
        return False
    return True


def favorites() -> list[str]:
    return load_state()["favorites"]


def set_favorite(vm_id: str, wanted: bool) -> bool:
    """Make `vm_id` a favorite or not; True when that changed anything.

    Raises OSError when the change could not be saved, rather than report a
    favorite that the next start would not show.
    """
    with state_lock():
        record = load_state()
        present = vm_id in record["favorites"]
        if present == wanted:
            return False
        if wanted:
            record["favorites"].append(vm_id)
        else:
            record["favorites"].remove(vm_id)
        save_state(record, strict=True)
    return True


def toggle_favorite(vm_id: str) -> bool:
    """Flip the favorite mark; True when it is now a favorite.

    Raises OSError, as set_favorite does, when the flip was not saved.
    """
    with state_lock():
        now = vm_id not in favorites()
        set_favorite(vm_id, now)
    return now


def recents() -> list[dict]:
    return load_state()["recents"]


def remember_launch(vm_id: str, when: datetime | None = None) -> None:
    moment = (when or datetime.now(timezone.utc)).replace(microsecond=0)
    try:
        with state_lock():
            record = load_state()
            record["recents"] = [{"id": vm_id, "at": moment.isoformat()}] + [
                row for row in record["recents"] if row["id"] != vm_id
            ][:MAX_RECENTS - 1]
            save_state(record)
    except OSError:
        pass        # a launch that worked is not undone by a busy state file


def ordered(machines: list[VmDefinition]) -> list[VmDefinition]:
    """Favorites first, then the most recently launched, then by name."""
    record = load_state()
    favorite = set(record["favorites"])
    recent = {row["id"]: index for index, row in enumerate(record["recents"])}
    return sorted(machines, key=lambda vm: (
        vm.id not in favorite,
        recent.get(vm.id, MAX_RECENTS),
        vm.name.casefold(),
        vm.id,
    ))
