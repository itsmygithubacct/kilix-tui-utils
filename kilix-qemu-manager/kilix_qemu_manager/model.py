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
* a per-VM QMP socket is added under ``$XDG_RUNTIME_DIR/kilix-qemu``;
* another VM's control files are never kept. A ``-pidfile`` or a QMP/monitor
  socket that belongs to another catalogued VM, or that lies in the manager's
  runtime directory under another VM's name, is replaced by this VM's own (a
  pidfile) or removed (a socket), with a note. A ``-chardev`` or other device
  bound to such a socket is refused, because other options refer to it by id.
  A pidfile or socket that belongs to no catalogued VM and lies outside the
  runtime directory is the user's (or a build's) own and is kept. Paths are
  compared canonically (:func:`canonical`), so a symlinked spelling of the
  runtime directory is still the runtime directory. A ``-pidfile`` on this
  VM's own QMP socket path is replaced by its own pidfile.

What a hand-edited definition may open is the loader's business
(``store.control_conflicts``).

Anything the manager cannot represent faithfully is refused rather than
guessed at: stdio-bound monitors and serial ports, ``-nographic``,
configuration it cannot see (``-readconfig``), user or root changes, and
option values that are themselves options this module acts on.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

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
    """Where QMP sockets, assigned pidfiles and launch markers live.

    ``$XDG_RUNTIME_DIR/kilix-qemu``; without an absolute XDG_RUNTIME_DIR, the
    fallback is ``<tmp>/kilix-qemu-<uid>``. Either one is used for a launch
    only after :func:`ensure_runtime_dir` has proved it is this user's own.
    """
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    if base and os.path.isabs(base):
        return os.path.join(base, "kilix-qemu")
    return os.path.join(tempfile.gettempdir(), f"kilix-qemu-{os.getuid()}")


class RuntimeDirError(OSError):
    """The runtime directory exists but is not safely this user's."""


def ensure_runtime_dir() -> str:
    """Create the runtime directory 0700, or prove an existing one is safe.

    A shared temporary directory lets another user create the fallback
    first and plant sockets in it, so the directory must be a real directory
    (not a symlink), owned by this user, with no group or other access.
    """
    path = runtime_dir()
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise RuntimeDirError(f"cannot create runtime directory {path}: "
                              f"{error.strerror or error}") from error
    if problem := _runtime_dir_problem(path):
        raise RuntimeDirError(problem)
    return path


def _runtime_dir_problem(path: str) -> str:
    """Why `path` is not safely this user's runtime directory; '' if it is.

    Status reads apply the same rule through an open descriptor, in
    :func:`reach`, and treat a missing directory as holding nothing.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return f"runtime directory {path} is missing"
    except OSError as error:
        return f"cannot use runtime directory {path}: {error}"
    return _dir_info_problem(path, info)


def _dir_info_problem(path: str, info: os.stat_result) -> str:
    if not stat.S_ISDIR(info.st_mode):
        return f"runtime directory {path} is not a directory (or is a symlink)"
    if info.st_uid != os.getuid():
        return f"runtime directory {path} belongs to uid {info.st_uid}, not you"
    if info.st_mode & 0o077:
        return (f"runtime directory {path} is mode "
                f"{stat.S_IMODE(info.st_mode):04o}; it must be 0700")
    return ""


def canonical(path: str) -> str:
    """One spelling per file, for comparing control paths.

    Symlinks resolve, in the directories (``/var/run`` for ``/run``) and in
    the final component, and so do ``.`` and ``..``, so two spellings of one
    socket or pidfile compare equal. A missing final component is kept as
    written. This is for telling whether two paths are one file (import and
    the loader); where a name lives is :func:`in_runtime_dir`'s question.
    """
    return os.path.realpath(path)


def in_runtime_dir(path: str) -> bool:
    """True when `path`, as written, names a file directly in the runtime dir.

    Decided by the name alone: the directory part of `path`, normalised
    (``.``, ``..`` and doubled slashes, no symlink lookup), is the runtime
    directory's name, normalised the same way. Nothing is resolved, so there
    is nothing for whoever can rename things in ``<tmp>`` to race. A symlink
    in the runtime directory is therefore still "in" it, wherever it points.

    A spelling of the runtime directory through a symlink (``/var/run`` for
    ``/run``) is not "in" it by name; :func:`reach` still recognises it by
    the directory it opens. The loader, which only decides who may be listed,
    also compares canonically (:func:`runtime_dir_alias`).
    """
    head, tail = os.path.split(path)
    if tail in ("", ".", ".."):         # names a directory, not a file in one
        return False
    return os.path.normpath(head or ".") == os.path.normpath(runtime_dir())


def runtime_dir_alias(path: str) -> bool:
    """`path` is in the runtime directory by name, or canonically.

    For the loader and import, which compare canonical paths and only ever
    refuse more when this says yes. Status and controls never use it.
    """
    if in_runtime_dir(path):
        return True
    head, tail = os.path.split(path)
    if tail in ("", ".", ".."):
        return False
    return canonical(head or ".") == canonical(runtime_dir())


# How many symlinks one walk follows, as the kernel's own limit.
MAX_SYMLINKS = 40
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _shared(info: os.stat_result) -> bool:
    """Group or others may create names here (with the sticky bit)."""
    return bool(info.st_mode & 0o022)


def _walk_problem(where: str, info: os.stat_result) -> str:
    """Why a directory the walk looks names up in may not be trusted."""
    if info.st_uid not in (0, os.getuid()):
        return f"{where}, which belongs to uid {info.st_uid}"
    if _shared(info) and not info.st_mode & stat.S_ISVTX:
        return f"{where}, which is mode {stat.S_IMODE(info.st_mode):04o}"
    return ""


@dataclass
class _Found:
    """Where a walk ended: the directory that holds the final name."""
    fd: int
    name: str
    info: os.stat_result | None         # lstat of the name; None if missing
    via_link: bool                      # the final name came from a symlink


def _walk(path: str, *, follow_last: bool, avoid: tuple | None = None,
          ) -> _Found | None:
    """Walk absolute `path` from ``/``, one component at a time, through
    directory descriptors; None when a directory on the way is missing.

    Every directory a name is looked up in is checked through its own
    descriptor: it must be this user's or root's, and not writable by group
    or others unless it is sticky (``/tmp``). In a sticky shared directory
    the next name must be this user's or root's too, because anyone could
    have made it. Directories are opened ``O_NOFOLLOW``, so a name checked as
    a directory cannot become a symlink before it is opened; a symlink is
    followed only where it was found under those rules, by reading it
    through the descriptor and walking its target the same way. `avoid` is
    the runtime directory's (st_dev, st_ino): a walk that would look up a
    name below a directory inside it is refused, because the manager keeps
    nothing in subdirectories there.

    Raises :class:`RuntimeDirError` with the reason when the walk may not
    go on. The caller closes the returned descriptor.
    """
    if not os.path.isabs(path):
        raise RuntimeDirError(f"{path} is not an absolute path")
    pending = [part for part in reversed(path.split("/")) if part]
    fd = os.open("/", _DIR_FLAGS)
    where, links, via_link = "/", 0, False
    try:
        if not pending:
            raise RuntimeDirError(f"{path} names a directory, not a file")
        while True:
            part = pending.pop()
            if part == ".":
                if not pending:
                    raise RuntimeDirError(f"{path} names a directory, "
                                          "not a file")
                continue
            info_dir = os.fstat(fd)
            if problem := _walk_problem(where, info_dir):
                raise RuntimeDirError(
                    f"{path} is reached through {problem}; another user "
                    "can change that, so it is not used")
            if part == "..":
                if not pending:
                    raise RuntimeDirError(f"{path} names a directory, "
                                          "not a file")
                parent = os.open("..", _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd, where = parent, os.path.dirname(where.rstrip("/")) or "/"
                continue
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            except OSError as error:
                raise RuntimeDirError(f"cannot read {path}: "
                                      f"{error.strerror or error}") from error
            here = os.path.join(where, part)
            if (info is not None and _shared(info_dir)
                    and info.st_uid not in (0, os.getuid())):
                raise RuntimeDirError(
                    f"{here} was made by uid {info.st_uid} in the shared "
                    f"directory {where}, so it is not used")
            last = not pending
            if avoid is not None and _identity(info_dir) == avoid:
                if not last:
                    raise RuntimeDirError(
                        f"{path} is below the runtime directory "
                        f"{runtime_dir()}; the manager keeps nothing in "
                        "subdirectories there")
                if info is not None and stat.S_ISLNK(info.st_mode):
                    raise RuntimeDirError(
                        f"{path} is a symlink in the runtime directory "
                        f"{runtime_dir()}; the manager makes none there, so "
                        "it does not follow one")
            if info is None:
                if last:
                    return _Found(fd, part, None, via_link)
                os.close(fd)
                fd = -1
                return None
            if stat.S_ISLNK(info.st_mode) and (follow_last or not last):
                links += 1
                if links > MAX_SYMLINKS:
                    raise RuntimeDirError(f"{path}: too many symlinks")
                target = os.readlink(part, dir_fd=fd)
                if not target:
                    raise RuntimeDirError(f"{here} is an empty symlink")
                if last:
                    via_link = True
                pending.extend(p for p in reversed(target.split("/")) if p)
                if target.startswith("/"):
                    root = os.open("/", _DIR_FLAGS)
                    os.close(fd)
                    fd, where = root, "/"
                if not pending:
                    raise RuntimeDirError(f"{path} names a directory, "
                                          "not a file")
                continue
            if last:
                return _Found(fd, part, info, via_link)
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeDirError(f"{here} is not a directory")
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                os.close(fd)
                fd = -1
                return None
            except OSError as error:
                raise RuntimeDirError(f"cannot open {here}: "
                                      f"{error.strerror or error}") from error
            os.close(fd)
            fd, where = child, here
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


def _open_runtime_dir() -> int:
    """The runtime directory, opened through :func:`_walk` and never through
    a symlink in its own name; -1 when it (or a directory above it) is
    missing. Not yet checked: :func:`_dir_info_problem` does that."""
    directory = runtime_dir()
    found = _walk(directory, follow_last=False)
    if found is None:
        return -1
    try:
        if found.info is None:
            return -1
        if stat.S_ISLNK(found.info.st_mode) or not stat.S_ISDIR(
                found.info.st_mode):
            raise RuntimeDirError(f"runtime directory {directory} is not a "
                                  "directory (or is a symlink)")
        try:
            return os.open(found.name, _DIR_FLAGS, dir_fd=found.fd)
        except FileNotFoundError:
            return -1
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise RuntimeDirError(f"runtime directory {directory} is not "
                                      "a directory (or is a symlink)") from error
            raise RuntimeDirError(f"cannot use runtime directory {directory}: "
                                  f"{error.strerror or error}") from error
    finally:
        os.close(found.fd)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


@contextlib.contextmanager
def _checked(path: str) -> Iterator[_Found | None]:
    """The directory descriptor and name `path` may be used through; None
    when there is nothing there. Raises :class:`RuntimeDirError` with the
    reason when it must not be used. See :func:`reach`."""
    directory = runtime_dir()
    runtime_fd = -1
    found: _Found | None = None
    try:
        try:
            runtime_fd = _open_runtime_dir()
        except RuntimeDirError:
            if in_runtime_dir(path):
                raise
            runtime_fd = -1             # not ours to judge from here
        if in_runtime_dir(path):
            # Decided by the name as written: the runtime directory itself,
            # opened once, checked and used through that descriptor.
            if runtime_fd < 0:
                yield None
                return
            if problem := _dir_info_problem(directory, os.fstat(runtime_fd)):
                raise RuntimeDirError(problem)
            name = os.path.basename(path)
            try:
                info = os.stat(name, dir_fd=runtime_fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            except OSError as error:
                raise RuntimeDirError(f"cannot read {path}: "
                                      f"{error.strerror or error}") from error
            found = _Found(runtime_fd, name, info, False)
            runtime_fd = -1             # closed through `found` now
        else:
            avoid = (_identity(os.fstat(runtime_fd)) if runtime_fd >= 0
                     else None)
            found = _walk(path, follow_last=True, avoid=avoid)
            if found is None:
                yield None
                return
            if avoid is not None and _identity(os.fstat(found.fd)) == avoid:
                # Another spelling of the runtime directory: its rules.
                if found.via_link:
                    raise RuntimeDirError(
                        f"{path} is a symlink into the runtime directory "
                        f"{directory}; the manager does not follow it")
                if problem := _dir_info_problem(directory,
                                                os.fstat(found.fd)):
                    raise RuntimeDirError(problem)
        if found.info is not None and stat.S_ISLNK(found.info.st_mode):
            raise RuntimeDirError(
                f"{path} is a symlink; the manager makes none in its runtime "
                "directory, so it does not follow one")
        yield found if found.info is not None else None
    finally:
        if found is not None:
            os.close(found.fd)
        if runtime_fd >= 0:
            os.close(runtime_fd)


def proc_fd_dir(fd: int) -> str:
    """``/proc/self/fd/N``: a path that reaches what `fd` holds."""
    return f"/proc/self/fd/{fd}"


@contextlib.contextmanager
def reach(path: str) -> Iterator[str | None]:
    """Where to open or connect to `path` for a status read or a control.

    Yields the path to use, or None when there is nothing there to read.
    Raises :class:`RuntimeDirError` with the reason when it must not be
    used at all. The yielded path is good only inside the ``with`` block.

    Every path is reached the same way: never by handing the name to the
    kernel, but through a descriptor of the directory that holds it,
    opened by a walk from ``/`` that checks each directory on the way
    (:func:`_walk`), and then as ``/proc/self/fd/N/<name>``. So no directory
    is looked up twice, and none can be swapped between the check and the
    use.

    * In the runtime directory (decided by the name as written, by
      :func:`in_runtime_dir`; or, for another spelling, by the directory the
      walk ends in), the directory must be a real directory of this user's,
      mode 0700 (the rule :func:`ensure_runtime_dir` applies to writes),
      and the name must not be a symlink: the manager makes none there, so
      one it finds is not its own and is never followed. A path elsewhere
      that is a symlink into the runtime directory is refused the same way,
      and so is a path below a subdirectory of it.
    * Anywhere else, every directory on the way must be this user's or
      root's and not writable by another user unless sticky, and in a sticky
      shared directory (``/tmp``) each name must be this user's or root's.
      So an earlier runtime directory that another user re-created, or a
      directory of theirs, is refused. A symlink is followed only where the
      same rules held.

    Without ``/proc/self/fd`` the directory cannot be reached through its
    descriptor, and the path is refused rather than reached by name.
    """
    with _checked(path) as found:
        if found is None:
            yield None
            return
        through = proc_fd_dir(found.fd)
        if not os.path.isdir(through):
            raise RuntimeDirError(
                f"cannot reach {path} through its checked directory: "
                "/proc/self/fd is not available, so it is not used")
        yield f"{through}/{found.name}"


def reach_problem(path: str) -> str:
    """Why `path` may not be read, connected to or launched with; '' when
    it may. Only checks, so it needs no ``/proc``."""
    try:
        with _checked(path):
            return ""
    except RuntimeDirError as error:
        return str(error)


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
    "async-teardown", "localtime", "no-quit", "singlestep", "mem-prealloc",
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
    "serial", "monitor", "qmp", "qmp-pretty", "parallel", "debugcon", "gdb",
})
# Monitors that are whole on one option, so one bound to another VM's socket
# can be dropped without breaking a reference elsewhere in the argv.
MONITOR_OPTIONS = frozenset({"qmp", "qmp-pretty", "monitor"})
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
        elif item.name == "pflash":
            files.append(_absolute(item.value, cwd))
        elif item.name == "blockdev":
            if item.value.lstrip().startswith("{"):
                try:
                    data = json.loads(item.value)
                except (ValueError, RecursionError):
                    data = {}
                paths = _json_blockdev_files(data)
            else:
                paths = _dotted_blockdev_files(_keys(item.value))
            files.extend(_absolute(path, cwd) for path in paths)
    seen: set[str] = set()
    return [path for path in files if not (path in seen or seen.add(path))]


_TRUE = ("on", "yes", "true")


def _json_blockdev_files(node: Any, depth: int = 0) -> list[str]:
    """File names in a JSON -blockdev and the "file" nodes nested under it."""
    if not isinstance(node, dict) or depth > 8 or node.get("read-only") is True:
        return []
    found = []
    if isinstance(node.get("filename"), str) and node["filename"]:
        found.append(node["filename"])
    return found + _json_blockdev_files(node.get("file"), depth + 1)


def _dotted_blockdev_files(keys: dict[str, str]) -> list[str]:
    """``filename=`` and ``file.filename=`` (any depth) of a dotted -blockdev."""
    found = []
    for key, value in keys.items():
        if key != "filename" and not key.endswith(".filename"):
            continue
        prefix = key[: -len("filename")]
        # A read-only node, or one under a read-only parent, takes no write lock.
        parts = prefix.rstrip(".").split(".") if prefix else []
        ancestors = [".".join(parts[:index]) for index in range(len(parts) + 1)]
        if any(keys.get(f"{scope}.read-only" if scope else "read-only") in _TRUE
               for scope in ancestors):
            continue
        if value:
            found.append(value)
    return found


def control_paths(args: Sequence[str], cwd: str) -> set[str]:
    """Every pidfile and unix socket path an argv opens, made absolute."""
    found: set[str] = set()
    try:
        items = parse_options(args)
    except DefinitionError:
        return found
    for item in items:
        if not isinstance(item, Option) or item.value is None:
            continue
        if item.name == "pidfile":
            found.add(_absolute(item.value, cwd))
        elif (path := _socket_path(item.name, item.value)):
            found.add(_absolute(path, cwd))
    return found


def _socket_path(name: str, value: str) -> str | None:
    """The unix socket an option binds: ``unix:PATH`` or a chardev ``path=``."""
    if name == "chardev":
        parts = split_keys(value)
        if parts and parts[0] == "socket":
            return _keys(value).get("path") or None
        return None
    return unix_path(value)


@dataclass
class Normalised:
    argv: list[str]
    pidfile: str
    qmp: str
    notes: list[str] = field(default_factory=list)
    name: str = ""


def text_problem(value: str) -> str:
    """Why `value` cannot be a path, an argv word or a shown name; '' if it can.

    The OS cannot take a NUL in a path (``os.path.realpath`` raises
    ValueError), and text that is not UTF-8 cannot be printed or rendered.
    """
    if "\0" in value:
        return "contains a NUL byte"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "is not valid UTF-8 text"
    return ""


def check_binary(binary: str) -> None:
    if not binary:
        raise DefinitionError("no QEMU binary was given")
    if problem := text_problem(binary):
        raise DefinitionError(f"the QEMU binary {problem}")
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
    claimed: dict[str, str] | None = None,
) -> Normalised:
    """Return the argv this manager will launch, with every change noted.

    ``claimed`` maps the pidfile and QMP paths of the other catalogued VMs to
    their ids; see the module notes for what happens to them.
    """
    check_binary(binary)
    if not ID_PATTERN.fullmatch(vm_id):
        raise DefinitionError(f"{vm_id!r} is not a valid VM id")
    for word in args:
        if not isinstance(word, str):
            raise DefinitionError("every argument must be a string")
        if "\0" in word or "\n" in word:
            raise DefinitionError("an argument contains a NUL or newline")
        if text_problem(word):
            raise DefinitionError("an argument is not valid UTF-8 text")
    items = parse_options(list(args))
    kept: list[Option | str] = []
    notes: list[str] = []
    pidfile = ""
    name = ""
    own_qmp = os.path.normpath(qmp_path(vm_id))
    own_pid = os.path.normpath(pid_path(vm_id))
    own_spec = qmp_spec(own_qmp)
    has_own_qmp = False
    # Paths are compared canonically (symlinked directories resolved), so
    # another spelling of a file is still that file.
    claimed = {canonical(path): owner
               for path, owner in (claimed or {}).items() if owner != vm_id}
    runtime = canonical(runtime_dir())

    def foreign(path: str, mine: str) -> str:
        """Whose control file `path` is, when it is not this VM's `mine`."""
        path = canonical(_absolute(path, cwd))
        if path == canonical(mine):
            return ""
        if path in claimed:
            return f"VM {claimed[path]}'s"
        if os.path.dirname(path) == runtime:
            return "another VM's"      # the manager's own namespace
        return ""

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
            if canonical(pidfile) == canonical(own_qmp):
                notes.append(f"replaced -pidfile {pidfile}: it is this VM's "
                             f"QMP socket path; assigned {own_pid}")
                pidfile = own_pid
            elif owner := foreign(pidfile, own_pid):
                notes.append(f"replaced -pidfile {pidfile}: it is {owner} "
                             f"pidfile; assigned {own_pid}")
                pidfile = own_pid
            elif pidfile != item.value:
                notes.append(f"made -pidfile absolute: {pidfile}")
            else:
                notes.append(f"kept -pidfile {pidfile}")
            kept.append(Option(item.word, item.name, pidfile))
            continue
        socket_path = (_socket_path(item.name, item.value)
                       if item.value is not None else None)
        if socket_path:
            absolute = os.path.normpath(_absolute(socket_path, cwd))
            own = canonical(absolute) == canonical(own_qmp)
            if own and item.name == "qmp":
                if has_own_qmp:
                    notes.append(f"removed a second -qmp on {own_qmp}")
                    continue
                has_own_qmp = True
                if item.value != own_spec:
                    notes.append(f"rewrote -qmp {item.value} as {own_spec}")
                kept.append(Option(item.word, item.name, own_spec))
                continue
            if own:
                raise DefinitionError(
                    f"{item.word} {item.value} binds this VM's QMP socket path, "
                    "which the manager's own -qmp needs")
            if owner := foreign(absolute, own_qmp):
                if item.name in MONITOR_OPTIONS:
                    notes.append(f"removed {item.word} {item.value}: it is "
                                 f"{owner} socket; this VM gets its own")
                    continue
                raise DefinitionError(
                    f"{item.word} {item.value} binds {owner} socket {absolute}; "
                    "other options may refer to it, so it cannot simply be "
                    "dropped — edit the argv")
        if item.name == "name" and item.value:
            name = split_keys(item.value)[0]
            if name.startswith("guest="):
                name = name[len("guest="):]
        kept.append(item)
    if not pidfile:
        pidfile = own_pid
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
        if (not isinstance(name, str) or not name.strip() or "\n" in name
                or text_problem(name)):
            raise DefinitionError("invalid display name")
        if not isinstance(qemu, str):
            raise DefinitionError("invalid qemu binary")
        check_binary(qemu)
        if not isinstance(argv, list) or not all(
                isinstance(word, str) for word in argv):
            raise DefinitionError("argv must be a list of strings")
        # A hand-edited file can hold what import refuses. A NUL makes every
        # os.path call raise, so it is refused here, as damage to this file.
        pidfile, qmp = data.get("pidfile"), data.get("qmp")
        for label, value in (("pidfile", pidfile), ("qmp", qmp),
                             ("cwd", data.get("cwd"))):
            if isinstance(value, str) and (problem := text_problem(value)):
                raise DefinitionError(f"{label} {problem}")
        for index, word in enumerate(argv):
            if problem := text_problem(word):
                raise DefinitionError(f"argv word {index + 1} {problem}")
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
        if not isinstance(notes, str) or text_problem(notes):
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
