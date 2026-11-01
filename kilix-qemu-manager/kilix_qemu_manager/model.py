"""VM definitions, where they live, and how an imported argv is normalised.

QEMU keeps no registry of machines, so this manager keeps one: one JSON file
per VM under ``$XDG_CONFIG_HOME/kilix-qemu/vms/`` (``KILIX_QEMU_HOME``
replaces the whole root, which is also how the tests keep their hands off the
real one). A definition is an argv list, never a shell string.

Import is the one place an argv is changed, and every change is reported:

* ``-daemonize`` is removed, because a daemon leaves the Kilix tab at once;
* a ``-display`` other than gtk or sdl is removed, because only a window can
  be streamed into a tab;
* an existing ``-pidfile`` is kept, otherwise one is assigned;
* a per-VM QMP socket is added under ``$XDG_RUNTIME_DIR/kilix-qemu``.

Anything the manager cannot represent faithfully is refused rather than
guessed at: stdio-bound monitors and serial ports, ``-nographic``,
configuration it cannot see (``-readconfig``), user or root changes, and
option values that are themselves options this module acts on.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

SCHEMA = 1
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}\Z")
USER_PATTERN = re.compile(r"[a-z_][a-z0-9_.-]{0,31}\Z")
BINARY_PATTERN = re.compile(r"qemu-system-[a-z0-9_]+\Z")
# The longest AF_UNIX path Linux accepts, including the terminating NUL.
UNIX_PATH_MAX = 108


class DefinitionError(ValueError):
    """A definition or an argv this manager cannot represent faithfully."""


# ── where things live ────────────────────────────────────────────────────────


def config_root() -> str:
    if override := os.environ.get("KILIX_QEMU_HOME"):
        return override
    base = (os.environ.get("XDG_CONFIG_HOME")
            or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, "kilix-qemu")


def vms_dir() -> str:
    return os.path.join(config_root(), "vms")


def state_path() -> str:
    """Favorites and recents: state, not configuration."""
    if override := os.environ.get("KILIX_QEMU_HOME"):
        return os.path.join(override, "state.json")
    base = (os.environ.get("XDG_STATE_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "state"))
    return os.path.join(base, "kilix-qemu", "state.json")


def runtime_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    if base and os.path.isabs(base):
        return os.path.join(base, "kilix-qemu")
    return os.path.join(tempfile.gettempdir(), f"kilix-qemu-{os.getuid()}")


def qmp_path(vm_id: str) -> str:
    return os.path.join(runtime_dir(), f"{vm_id}.qmp")


def pid_path(vm_id: str) -> str:
    return os.path.join(runtime_dir(), f"{vm_id}.pid")


def qemu_escape(value: str) -> str:
    """QEMU's option syntax doubles a literal comma."""
    return value.replace(",", ",,")


def qmp_spec(path: str) -> str:
    return f"unix:{qemu_escape(path)},server=on,wait=off"


def slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", name.casefold()).strip("-._")
    return text[:48].rstrip("-._") or "vm"


# ── QEMU's option grammar, as far as this module needs it ────────────────────

# Options that take no value. Every other option consumes the next word, which
# is how QEMU's own parser works (qemu-options.hx, HAS_ARG = 0).
FLAGS = frozenset({
    "h", "help", "version", "S", "s", "snapshot", "nographic", "curses",
    "portrait", "full-screen", "win2k-hack", "no-fd-bootchk", "no-acpi",
    "no-hpet", "no-reboot", "no-shutdown", "enable-kvm", "xen-attach",
    "xen-domid-restrict", "no-user-config", "nodefaults", "nodefconfig",
    "daemonize", "preconfig", "usb", "sdl", "alt-grab", "ctrl-grab",
    "semihosting", "only-migratable", "enable-fips", "old-param", "no-frame",
    "rtc-td-hack", "enable-sync-profile", "perfmap", "jitdump",
    "async-teardown", "localtime", "no-quit", "singlestep",
})
# Options this module acts on. A value that spells one of these is refused:
# it means the option table above disagreed with the argv's author, and a
# guess would either keep a -daemonize or drop something real.
ACTED_ON = frozenset({
    "daemonize", "display", "pidfile", "qmp", "qmp-pretty", "nographic",
    "readconfig", "runas", "chroot", "run-with", "monitor", "serial",
    "chardev", "curses", "drive", "hda", "hdb", "hdc", "hdd", "blockdev",
})
STDIO_OPTIONS = frozenset({
    "serial", "monitor", "qmp", "qmp-pretty", "parallel", "debugcon",
})
REFUSED_FLAGS = {
    "nographic": "-nographic puts the serial console and monitor on stdio, "
                 "which a Kilix tab does not connect to the guest",
    "readconfig": "-readconfig loads options this manager cannot see",
    "runas": "-runas changes the user the VM runs as",
    "chroot": "-chroot changes the VM's root directory",
}
DISPLAYS_KEPT = ("gtk", "sdl")


def _option_name(word: str) -> str | None:
    if len(word) > 1 and word.startswith("-") and word != "--":
        return word.lstrip("-") if word.startswith("--") else word[1:]
    return None


@dataclass(frozen=True)
class Option:
    word: str                 # the option exactly as written, e.g. "--m"
    name: str                 # its name without dashes, e.g. "m"
    value: str | None = None


def parse_options(args: Sequence[str]) -> list[Option | str]:
    """Split QEMU arguments into options and positional words."""
    items: list[Option | str] = []
    index = 0
    while index < len(args):
        word = args[index]
        name = _option_name(word)
        if name is None:
            items.append(word)
            index += 1
            continue
        if "=" in name:
            raise DefinitionError(
                f"{word.split('=', 1)[0]}=… is not QEMU option syntax; "
                "give the value as the next argument")
        if name in FLAGS:
            items.append(Option(word, name))
            index += 1
            continue
        if index + 1 >= len(args):
            raise DefinitionError(f"{word} needs a value")
        value = args[index + 1]
        if (_option_name(value) or "") in ACTED_ON:
            raise DefinitionError(
                f"the value of {word} is {value!r}; refusing to guess "
                "whether that is a value or an option")
        items.append(Option(word, name, value))
        index += 2
    return items


def _flatten(items: Iterable[Option | str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, Option):
            out.append(item.word)
            if item.value is not None:
                out.append(item.value)
        else:
            out.append(item)
    return out


def split_keys(value: str) -> list[str]:
    """Split a QEMU key=value list on single commas; ',,' is a literal one."""
    parts, current, index = [], [], 0
    while index < len(value):
        char = value[index]
        if char == ",":
            if index + 1 < len(value) and value[index + 1] == ",":
                current.append(",")
                index += 2
                continue
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _keys(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in split_keys(value):
        key, separator, item = part.partition("=")
        if separator:
            out.setdefault(key, item)
    return out


def _is_stdio(name: str, value: str) -> bool:
    if name == "chardev":
        return split_keys(value)[0] == "stdio"
    if name in STDIO_OPTIONS:
        head = split_keys(value)[0]
        return head in ("stdio", "mon:stdio") or head.startswith("stdio")
    return False


def _absolute(path: str, cwd: str) -> str:
    return path if os.path.isabs(path) else os.path.normpath(
        os.path.join(cwd, path))


def unix_path(spec: str) -> str | None:
    """The socket path of a ``unix:PATH,…`` chardev, or None."""
    if not spec.startswith("unix:"):
        return None
    return split_keys(spec[len("unix:"):])[0] or None


def disk_files(args: Sequence[str], cwd: str) -> list[str]:
    """Writable local image files an argv opens — the ones QEMU locks."""
    files: list[str] = []
    try:
        items = parse_options(args)
    except DefinitionError:
        return files
    for item in items:
        if isinstance(item, str):
            if not item.startswith("-"):
                files.append(_absolute(item, cwd))
            continue
        if item.value is None:
            continue
        if item.name in ("hda", "hdb", "hdc", "hdd"):
            files.append(_absolute(item.value, cwd))
        elif item.name == "drive":
            keys = _keys(item.value)
            path = keys.get("file", "")
            if (not path or keys.get("readonly") in ("on", "yes", "true")
                    or keys.get("media") == "cdrom"):
                continue
            if ":" in path and not os.path.isabs(path):
                continue            # a protocol (nbd:, json:), not a file
            files.append(_absolute(path, cwd))
        elif item.name == "blockdev":
            path = ""
            if item.value.lstrip().startswith("{"):
                try:
                    data = json.loads(item.value)
                except ValueError:
                    data = {}
                if isinstance(data, dict) and data.get("read-only") is not True:
                    path = str(data.get("filename") or "")
            else:
                keys = _keys(item.value)
                if keys.get("read-only") not in ("on", "yes", "true"):
                    path = keys.get("filename", "")
            if path:
                files.append(_absolute(path, cwd))
    seen: set[str] = set()
    return [path for path in files if not (path in seen or seen.add(path))]


@dataclass
class Normalised:
    argv: list[str]
    pidfile: str
    qmp: str
    notes: list[str] = field(default_factory=list)
    name: str = ""


def check_binary(binary: str) -> None:
    if not binary:
        raise DefinitionError("no QEMU binary was given")
    if any(char.isspace() for char in binary):
        raise DefinitionError(
            "the QEMU binary contains whitespace; give the command as "
            "separate words after --, not as one string")
    if not BINARY_PATTERN.fullmatch(os.path.basename(binary)):
        raise DefinitionError(
            f"{os.path.basename(binary)!r} is not a qemu-system-* binary")


def normalise(
    vm_id: str,
    binary: str,
    args: Sequence[str],
    *,
    cwd: str,
) -> Normalised:
    """Return the argv this manager will launch, with every change noted."""
    check_binary(binary)
    if not ID_PATTERN.fullmatch(vm_id):
        raise DefinitionError(f"{vm_id!r} is not a valid VM id")
    for word in args:
        if not isinstance(word, str):
            raise DefinitionError("every argument must be a string")
        if "\0" in word or "\n" in word:
            raise DefinitionError("an argument contains a NUL or newline")
    items = parse_options(list(args))
    kept: list[Option | str] = []
    notes: list[str] = []
    pidfile = ""
    name = ""
    own_qmp = qmp_path(vm_id)
    own_spec = qmp_spec(own_qmp)
    has_own_qmp = False
    for item in items:
        if isinstance(item, str):
            kept.append(item)
            continue
        if item.name in REFUSED_FLAGS:
            raise DefinitionError(REFUSED_FLAGS[item.name])
        if item.name == "run-with" and item.value:
            keys = _keys(item.value)
            if "user" in keys or "chroot" in keys:
                raise DefinitionError(
                    "-run-with user=/chroot= changes who or where the VM runs")
        if item.value is not None and _is_stdio(item.name, item.value):
            raise DefinitionError(
                f"{item.word} {item.value} is bound to stdio, which a Kilix "
                "tab does not connect to the guest")
        if item.name == "daemonize":
            notes.append("removed -daemonize: a daemon would leave its "
                         "Kilix tab immediately")
            continue
        if item.name == "curses":
            notes.append("removed -curses: only a gtk or sdl window can be "
                         "shown in a Kilix tab")
            continue
        if item.name == "display":
            backend = split_keys(item.value or "")[0]
            if backend in DISPLAYS_KEPT:
                kept.append(item)
            else:
                notes.append(
                    f"removed -display {item.value}: only gtk or sdl can be "
                    "shown in a Kilix tab")
            continue
        if item.name == "pidfile":
            if pidfile:
                raise DefinitionError("the argv names more than one -pidfile")
            pidfile = _absolute(item.value or "", cwd)
            kept.append(Option(item.word, item.name, pidfile))
            if pidfile != item.value:
                notes.append(f"made -pidfile absolute: {pidfile}")
            else:
                notes.append(f"kept -pidfile {pidfile}")
            continue
        if item.name == "qmp" and item.value == own_spec:
            has_own_qmp = True
        if item.name == "name" and item.value:
            name = split_keys(item.value)[0]
            if name.startswith("guest="):
                name = name[len("guest="):]
        kept.append(item)
    if not pidfile:
        pidfile = pid_path(vm_id)
        kept.append(Option("-pidfile", "pidfile", pidfile))
        notes.append(f"assigned -pidfile {pidfile}")
    if len(own_qmp.encode()) >= UNIX_PATH_MAX:
        raise DefinitionError(
            f"the QMP socket path is too long for a unix socket: {own_qmp}")
    if not has_own_qmp:
        kept.append(Option("-qmp", "qmp", own_spec))
        notes.append(f"added QMP socket {own_qmp}")
    return Normalised(_flatten(kept), pidfile, own_qmp, notes, name)


# ── the definition itself ────────────────────────────────────────────────────


@dataclass
class VmDefinition:
    id: str
    name: str
    qemu: str
    argv: list[str]
    pidfile: str
    qmp: str
    cwd: str = ""
    ssh_port: int | None = None
    ssh_user: str = ""
    notes: str = ""

    @property
    def command(self) -> list[str]:
        return [self.qemu, *self.argv]

    @property
    def has_ssh(self) -> bool:
        return bool(self.ssh_port and self.ssh_user)

    @property
    def disks(self) -> list[str]:
        return disk_files(self.argv, self.cwd or "/")

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "id": self.id,
            "name": self.name,
            "qemu": self.qemu,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "pidfile": self.pidfile,
            "qmp": self.qmp,
            "ssh": ({"port": self.ssh_port, "user": self.ssh_user}
                    if self.has_ssh else None),
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, data: Any) -> "VmDefinition":
        if not isinstance(data, dict):
            raise DefinitionError("a definition must be a JSON object")
        if data.get("schema") != SCHEMA:
            raise DefinitionError(f"unsupported schema {data.get('schema')!r}")
        vm_id = data.get("id")
        if not isinstance(vm_id, str) or not ID_PATTERN.fullmatch(vm_id):
            raise DefinitionError(f"invalid id {vm_id!r}")
        name = data.get("name")
        qemu = data.get("qemu")
        argv = data.get("argv")
        if not isinstance(name, str) or not name.strip() or "\n" in name:
            raise DefinitionError("invalid display name")
        if not isinstance(qemu, str):
            raise DefinitionError("invalid qemu binary")
        check_binary(qemu)
        if not isinstance(argv, list) or not all(
                isinstance(word, str) for word in argv):
            raise DefinitionError("argv must be a list of strings")
        pidfile, qmp = data.get("pidfile"), data.get("qmp")
        if not isinstance(pidfile, str) or not os.path.isabs(pidfile):
            raise DefinitionError("pidfile must be an absolute path")
        if not isinstance(qmp, str) or not os.path.isabs(qmp):
            raise DefinitionError("qmp must be an absolute path")
        # The recorded paths must be the ones the argv really uses, or the
        # manager would watch one socket while QEMU listens on another.
        if qmp_spec(qmp) not in argv:
            raise DefinitionError("argv does not open the recorded QMP socket")
        if pidfile not in argv:
            raise DefinitionError("argv does not write the recorded pidfile")
        if "-daemonize" in argv or "--daemonize" in argv:
            raise DefinitionError("argv contains -daemonize")
        cwd = data.get("cwd") or ""
        if not isinstance(cwd, str) or (cwd and not os.path.isabs(cwd)):
            raise DefinitionError("cwd must be an absolute path")
        ssh = data.get("ssh")
        port, user = None, ""
        if ssh is not None:
            if not isinstance(ssh, dict):
                raise DefinitionError("ssh must be an object or null")
            port, user = ssh.get("port"), ssh.get("user")
            if (not isinstance(port, int) or isinstance(port, bool)
                    or not 1 <= port <= 65535):
                raise DefinitionError("ssh port must be 1-65535")
            if not isinstance(user, str) or not USER_PATTERN.fullmatch(user):
                raise DefinitionError("invalid ssh user")
        notes = data.get("notes") or ""
        if not isinstance(notes, str):
            raise DefinitionError("notes must be text")
        return cls(vm_id, name.strip(), qemu, list(argv), pidfile, qmp, cwd,
                   port, user, notes)


def parse_ssh(value: str) -> tuple[int, str]:
    """``USER@PORT`` or ``PORT:USER`` → (port, user)."""
    user, separator, port = value.partition("@")
    if not separator:
        port, separator, user = value.partition(":")
    if not separator:
        raise DefinitionError("ssh forward must be USER@PORT")
    try:
        number = int(port)
    except ValueError as error:
        raise DefinitionError("ssh port must be a number") from error
    if not 1 <= number <= 65535:
        raise DefinitionError("ssh port must be 1-65535")
    if not USER_PATTERN.fullmatch(user):
        raise DefinitionError(f"invalid ssh user {user!r}")
    return number, user


# ── live status, as the backend reports it ───────────────────────────────────


@dataclass
class VmStatus:
    vm: VmDefinition
    state: str = "stopped"      # stopped | starting | running | paused | … |
                                # live (QEMU's own status word otherwise)
    pid: int = 0
    qmp_ok: bool = False
    disk_locked: str = ""       # the first locked image, if any
    pane_id: int = 0            # the Kilix pane that owns the QMP socket
    tabs_known: bool = False    # False when Kilix could not be asked
    favorite: bool = False
    last_launch: str = ""
    detail: str = ""

    @property
    def live(self) -> bool:
        return self.state != "stopped"

    @property
    def startable(self) -> bool:
        return self.state == "stopped"

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def paused(self) -> bool:
        return self.state in ("paused", "suspended")

    @property
    def outside_kilix(self) -> bool:
        return self.live and self.tabs_known and not self.pane_id

    @property
    def label(self) -> str:
        if self.outside_kilix and self.state != "starting":
            return "live, no Kilix tab"
        return self.state

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.vm.id,
            "name": self.vm.name,
            "state": self.state,
            "label": self.label,
            "pid": self.pid or None,
            "qmp": self.qmp_ok,
            "disk_locked": self.disk_locked or None,
            "kilix_pane": self.pane_id or None,
            "favorite": self.favorite,
            "last_launch": self.last_launch or None,
            "ssh": ({"port": self.vm.ssh_port, "user": self.vm.ssh_user}
                    if self.vm.has_ssh else None),
            "detail": self.detail or None,
        }
