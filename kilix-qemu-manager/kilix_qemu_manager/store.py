"""The catalog of VM definitions, and the one small favorites/recents record.

Definitions are configuration: one JSON file per VM, written only by an
explicit import, accept, or remove. Favorites and recents are state, kept the
way ``kilix_desk/durable.py`` keeps the desktop's record: one small file, an
atomic replace, write-on-change only, and a damaged or unwritable file that
degrades to "nothing remembered" rather than a crash.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
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


# ── definitions ──────────────────────────────────────────────────────────────


def _definition_path(vm_id: str) -> str:
    if not model.ID_PATTERN.fullmatch(vm_id):
        raise DefinitionError(f"{vm_id!r} is not a valid VM id")
    return os.path.join(model.vms_dir(), f"{vm_id}.json")


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
            with open(path, encoding="utf-8") as handle:
                vm = VmDefinition.from_json(json.load(handle))
        except RecursionError:
            problems.append(f"{name}: nested too deeply to read")
            continue
        except (OSError, ValueError) as error:
            problems.append(f"{name}: {error}")
            continue
        if f"{vm.id}.json" != name:
            problems.append(f"{name}: id {vm.id!r} does not match the file")
            continue
        found.append(vm)
    # Each VM's status and controls go through its own pidfile and QMP
    # socket. A definition whose argv opens another's (a hand edit, or a
    # clone made before import refused that) would act on the other VM.
    owners = claimed_paths(found)
    clean: list[VmDefinition] = []
    for vm in found:
        shared = sorted({owners[path] for path in
                         model.control_paths(vm.argv, vm.cwd or "/")
                         if owners.get(path, vm.id) != vm.id})
        if shared:
            problems.append(
                f"{vm.id}.json: its argv opens the pidfile or QMP socket of "
                f"{', '.join(shared)}; re-import it to give it its own")
            continue
        clean.append(vm)
    return clean, problems


def claimed_paths(machines: list[VmDefinition]) -> dict[str, str]:
    """Each catalogued VM's pidfile and QMP socket path, mapped to its id."""
    owners: dict[str, str] = {}
    for vm in machines:
        owners[os.path.normpath(vm.pidfile)] = vm.id
        owners[os.path.normpath(vm.qmp)] = vm.id
    return owners


def get(vm_id: str) -> VmDefinition | None:
    try:
        with open(_definition_path(vm_id), encoding="utf-8") as handle:
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


def _write_json(path: str, data: object) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
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
    with _STATE_LOCK:
        record = load_state()
        record["favorites"] = [item for item in record["favorites"]
                               if item != vm_id]
        record["recents"] = [row for row in record["recents"]
                             if row["id"] != vm_id]
        save_state(record)
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
        with open(model.state_path(), encoding="utf-8") as handle:
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
    with _STATE_LOCK:
        if clean == load_state():
            return False
        try:
            _write_json(model.state_path(), clean)
        except OSError:
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
    with _STATE_LOCK:
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
    with _STATE_LOCK:
        now = vm_id not in favorites()
        set_favorite(vm_id, now)
    return now


def recents() -> list[dict]:
    return load_state()["recents"]


def remember_launch(vm_id: str, when: datetime | None = None) -> None:
    moment = (when or datetime.now(timezone.utc)).replace(microsecond=0)
    with _STATE_LOCK:
        record = load_state()
        record["recents"] = [{"id": vm_id, "at": moment.isoformat()}] + [
            row for row in record["recents"] if row["id"] != vm_id
        ][:MAX_RECENTS - 1]
        save_state(record)


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
