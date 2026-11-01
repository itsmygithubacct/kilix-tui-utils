"""Liveness, launch, focus and QMP control for catalogued QEMU VMs.

A VM is live when any one of three independent witnesses says so: its
pidfile names a QEMU process of this user that opened this VM's files, its
QMP socket answers, or one of its writable disk images is locked. Launch
refuses while any witness is up, so a second copy is never opened over a
running one — not even one started outside Kilix.

Launching goes through the shared ``kilix_tui.kitty_rc`` helper: one new tab,
titled "QEMU: <name>", opened with ``--keep-focus``, running
``kilix run [--size WxH] <qemu argv>``. ``KILIX_IN_OVERLAY=1`` makes
``kilix run`` draw in that tab instead of relaunching itself into a second,
focus-taking one (the same guard Kilix's own desktop launcher sets).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import model, qmp, store
from .model import VmDefinition, VmStatus

LAUNCH_MARKER_SECONDS = 30.0

# Byte ranges QEMU's file-posix driver locks: permission bits from offset 100,
# shared-permission bits from offset 200 (block/file-posix.c).
_LOCK_START, _LOCK_LENGTH = 100, 200


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    message: str
    command: tuple[str, ...] = ()


QMP_ACTIONS: dict[str, tuple[str, str]] = {
    # action: (QMP command, past-tense label)
    "shutdown": ("system_powerdown", "Sent the ACPI power button to"),
    "pause": ("stop", "Paused"),
    "resume": ("cont", "Resumed"),
    "reset": ("system_reset", "Reset"),
    "poweroff": ("quit", "Powered off"),
}
DESTRUCTIVE = frozenset({"reset", "poweroff"})


def _default_rc():
    from kilix_tui import kitty_rc
    return kitty_rc


def _run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True,
                          check=False, timeout=timeout)


def _resolve(override: str | None, command: str,
             candidates: Sequence[str] = ()) -> str:
    if override:
        found = shutil.which(override)
        return found or os.path.abspath(os.path.expanduser(override))
    found = shutil.which(command)
    if found:
        return found
    for candidate in candidates:
        path = os.path.abspath(os.path.expanduser(candidate))
        if os.access(path, os.X_OK):
            return path
    return command


def disk_locked(path: str) -> bool:
    """True when another open file description holds a QEMU image lock.

    F_OFD_GETLK only asks; it takes no lock, so probing cannot disturb a
    running VM or one that is starting. Only regular files and block devices
    are opened, and without blocking: a FIFO would hang the refresh, and
    opening some character devices has side effects.
    """
    if not probe_safe(path):
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
                     | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        if not _image_mode(os.fstat(fd).st_mode):
            return False
        request = struct.pack("hhqqi4x", fcntl.F_WRLCK, os.SEEK_SET,
                              _LOCK_START, _LOCK_LENGTH, 0)
        answer = fcntl.fcntl(fd, fcntl.F_OFD_GETLK, request)
        return struct.unpack("hhqqi4x", answer)[0] != fcntl.F_UNLCK
    except (OSError, AttributeError, struct.error):
        return False
    finally:
        os.close(fd)


def _image_mode(mode: int) -> bool:
    return stat.S_ISREG(mode) or stat.S_ISBLK(mode)


def probe_safe(path: str) -> bool:
    """True for a path that is a regular file or block device right now."""
    try:
        return _image_mode(os.stat(path).st_mode)
    except OSError:
        return False


QMP_WORDS = ("-qmp", "--qmp")


def find_pane(tree: Any, qmp_path: str) -> Any | None:
    """The pane whose foreground command opens exactly this QMP socket.

    Every process in the pane's foreground group is considered, because the
    QEMU argv appears in ``kilix run``'s launcher and in its Python host, not
    necessarily in the last process kitty lists. The socket path is compared
    whole after QEMU's own key splitting, never as a substring and never by
    the (mutable) tab title. The path must be the value of a ``-qmp``
    option, so a pane that merely mentions the socket (``socat - unix:…``)
    is not mistaken for the VM's tab.
    """
    if tree is None:
        return None
    for pane in tree.panes:
        argvs = [process.argv for process in getattr(pane, "processes", ())]
        argvs.append(tuple(getattr(pane, "argv", ())))
        for argv in argvs:
            for index, word in enumerate(argv):
                if (index and argv[index - 1] in QMP_WORDS
                        and model.unix_path(word) == qmp_path):
                    return pane
    return None


class QemuManager:
    """Everything that touches processes, sockets and Kilix."""

    def __init__(
        self,
        *,
        rc: Any = None,
        kilix: str | None = None,
        qemu_img: str | None = None,
        runner: Callable[[Sequence[str], float],
                         subprocess.CompletedProcess[str]] = _run,
        proc_root: str = "/proc",
        launch_wait_seconds: float = 20.0,
        qmp_timeout: float = 1.0,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.rc = rc if rc is not None else _default_rc()
        source = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or str(
            Path.home() / "gpu_terminal")
        self.kilix = _resolve(
            kilix or os.environ.get("KILIX_LAUNCHER"), "kilix",
            (str(Path.home() / ".local/bin/kilix"),
             os.path.join(source, "kilix", "kilix")))
        self.qemu_img = _resolve(
            qemu_img or os.environ.get("KILIX_QEMU_IMG"), "qemu-img")
        self._runner = runner
        self.proc_root = proc_root
        self.launch_wait_seconds = max(0.0, float(launch_wait_seconds))
        self.qmp_timeout = qmp_timeout
        self.environ = environ

    # ── witnesses ────────────────────────────────────────────────────────────

    def _cmdline(self, pid: int) -> list[str]:
        try:
            with open(os.path.join(self.proc_root, str(pid), "cmdline"),
                      "rb") as handle:
                raw = handle.read()
        except OSError:
            return []
        return [part.decode("utf-8", "replace")
                for part in raw.split(b"\0") if part]

    def _owned(self, pid: int) -> bool:
        try:
            return os.stat(os.path.join(
                self.proc_root, str(pid))).st_uid == os.getuid()
        except OSError:
            return False

    def pid_of(self, vm: VmDefinition) -> int:
        """The live QEMU named by the pidfile, verified against its argv."""
        try:
            with open(vm.pidfile, encoding="ascii") as handle:
                pid = int(handle.read().strip() or 0)
        except (OSError, ValueError):
            return 0
        if pid <= 0 or not self._owned(pid):
            return 0
        argv = self._cmdline(pid)
        if not argv or not model.BINARY_PATTERN.fullmatch(
                os.path.basename(argv[0])):
            return 0
        # A recycled PID belongs to some other process; ours opened our files.
        if vm.pidfile not in argv and model.qmp_spec(vm.qmp) not in argv:
            return 0
        return pid

    def locked_disk(self, vm: VmDefinition) -> str:
        for path in vm.disks:
            if disk_locked(path):
                return path
        return ""

    def _marker(self, vm: VmDefinition) -> str:
        return os.path.join(model.runtime_dir(), f"{vm.id}.launch")

    def _launching(self, vm: VmDefinition) -> bool:
        try:
            age = time.time() - os.stat(self._marker(vm)).st_mtime
        except OSError:
            return False
        return 0 <= age < LAUNCH_MARKER_SECONDS

    def tree(self) -> tuple[Any | None, str]:
        """Kilix's page/pane tree, or None with the reason it is unknown."""
        try:
            if not self.rc.available():
                return None, "not inside Kilix"
            return self.rc.tree(), ""
        except Exception as error:      # Unavailable, or a malformed reply
            return None, str(error)

    def status(self, vm: VmDefinition, tree: Any = None, *,
               tabs_known: bool | None = None,
               state: dict | None = None) -> VmStatus:
        record = state if state is not None else store.load_state()
        result = VmStatus(
            vm,
            favorite=vm.id in record["favorites"],
            last_launch=next((row["at"] for row in record["recents"]
                              if row["id"] == vm.id), ""),
            tabs_known=tree is not None if tabs_known is None else tabs_known,
        )
        result.pid = self.pid_of(vm)
        try:
            result.state = qmp.status(vm.qmp, timeout=self.qmp_timeout)
            result.qmp_ok = True
        except qmp.QmpError as error:
            qmp_problem = str(error)
            result.disk_locked = self.locked_disk(vm)
            if result.pid:
                result.state = "live"
                result.detail = f"pid {result.pid}; QMP: {qmp_problem}"
            elif result.disk_locked:
                result.state = "live"
                result.detail = f"disk in use: {result.disk_locked}"
            elif self._launching(vm):
                result.state = "starting"
            else:
                result.state = "stopped"
        pane = find_pane(tree, vm.qmp)
        result.pane_id = int(getattr(pane, "id", 0) or 0) if pane else 0
        if result.state == "stopped" and result.pane_id:
            # A Kilix tab still runs this VM's command, though no other
            # witness is up yet (QEMU is starting, or slow to open QMP).
            # Opening a second copy beside it is never right.
            result.state = "starting"
            result.detail = (f"its Kilix tab (pane {result.pane_id}) is open; "
                             "QMP does not answer yet")
        return result

    def inventory(self) -> tuple[list[VmStatus], list[str], str]:
        machines, problems = store.load_all()
        machines = store.ordered(machines)
        tree, tree_error = self.tree() if machines else (None, "")
        record = store.load_state()
        return ([self.status(vm, tree, state=record) for vm in machines],
                problems, tree_error)

    # ── commands and previews ────────────────────────────────────────────────

    def launch_command(self, vm: VmDefinition,
                       size: str | None = None) -> list[str]:
        options = ["--size", size] if size else []
        return [self.kilix, "run", *options, *vm.command]

    def tab_argv(self, vm: VmDefinition, size: str | None = None) -> list[str]:
        return ["env", "KILIX_IN_OVERLAY=1", *self.launch_command(vm, size)]

    def ssh_argv(self, vm: VmDefinition) -> list[str]:
        return ["ssh", "-p", str(vm.ssh_port), f"{vm.ssh_user}@127.0.0.1"]

    def preview(self, vm: VmDefinition, action: str,
                size: str | None = None, pane_id: int = 0) -> tuple[str, ...]:
        """The exact command an action runs, for the confirmation screen."""
        if action == "launch":
            return ("kitten", "@", "launch", "--type=tab", "--tab-title",
                    f"QEMU: {vm.name}", "--keep-focus", "--",
                    *self.tab_argv(vm, size))
        if action == "focus":
            target = f"id:{pane_id}" if pane_id else f"qmp:{vm.qmp}"
            return ("kitten", "@", "focus-window", "--match", target)
        if action == "ssh":
            return ("kitten", "@", "launch", "--type=tab", "--tab-title",
                    f"SSH: {vm.name}", "--keep-focus", "--",
                    *self.ssh_argv(vm))
        if action in QMP_ACTIONS:
            message = json.dumps({"execute": QMP_ACTIONS[action][0]})
            return ("qmp", vm.qmp, message)
        raise ValueError(f"unsupported action: {action}")

    def qemu_img_argv(self, path: str) -> tuple[str, ...]:
        return (self.qemu_img, "info", "-U", "--output=json", path)

    # ── actions ──────────────────────────────────────────────────────────────

    def focus(self, vm: VmDefinition) -> OperationResult:
        tree, problem = self.tree()
        if tree is None:
            return OperationResult(False, f"Cannot ask Kilix for tabs: {problem}",
                                   self.preview(vm, "focus"))
        pane = find_pane(tree, vm.qmp)
        if pane is None:
            return OperationResult(
                False, f"{vm.name} is live, but no Kilix tab owns its QMP "
                       "socket.", self.preview(vm, "focus"))
        command = self.preview(vm, "focus", pane_id=pane.id)
        try:
            self.rc.focus_pane(pane.id)
        except Exception as error:
            return OperationResult(False, str(error), command)
        return OperationResult(True, f"Focused the {vm.name} tab.", command)

    def _claim_launch(self, vm: VmDefinition) -> bool:
        """Create the launch marker exclusively; False if another holds it."""
        marker = self._marker(vm)
        model.ensure_runtime_dir()
        if os.path.exists(marker) and not self._launching(vm):
            try:
                os.unlink(marker)       # a stale claim from a crashed launch
            except OSError:
                pass
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"{os.getpid()}\n")
        return True

    def _release_launch(self, vm: VmDefinition) -> None:
        try:
            os.unlink(self._marker(vm))
        except OSError:
            pass

    def launch(self, vm: VmDefinition, *, size: str | None = None,
               ) -> OperationResult:
        command = self.preview(vm, "launch", size)
        tree, _problem = self.tree()
        current = self.status(vm, tree)
        if current.state == "starting":
            where = f" ({current.detail})" if current.detail else ""
            return OperationResult(
                False, f"{vm.name} is already being launched{where}; not "
                       "opening a second copy.", command)
        if current.live:
            if current.pane_id:
                return self.focus(vm)
            where = (f"pid {current.pid}" if current.pid
                     else current.detail or current.state)
            return OperationResult(
                False, f"{vm.name} is already running outside Kilix ({where});"
                       " not opening a second copy.", command)
        if not self.rc.available():
            return OperationResult(
                False, "Open this manager inside Kilix before launching a VM.",
                command)
        try:
            model.ensure_runtime_dir()
        except model.RuntimeDirError as error:
            return OperationResult(
                False, f"Not launching {vm.name}: {error}.", command)
        if not self._claim_launch(vm):
            return OperationResult(
                False, f"{vm.name} is already being launched.", command)
        try:
            try:
                pane_id = self.rc.launch_tab(
                    self.tab_argv(vm, size), title=f"QEMU: {vm.name}",
                    cwd=vm.cwd or None, keep_focus=True)
            except Exception as error:
                return OperationResult(False, f"Kilix refused the tab: {error}",
                                       command)
            deadline = time.monotonic() + self.launch_wait_seconds
            while True:
                try:
                    state = qmp.status(vm.qmp, timeout=self.qmp_timeout)
                except qmp.QmpError:
                    state = ""
                if state:
                    store.remember_launch(vm.id)
                    where = f" (pane {pane_id})" if pane_id else ""
                    return OperationResult(
                        True, f"Opened {vm.name} in a new Kilix tab{where}; "
                              f"QEMU reports {state}.", command)
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            return OperationResult(
                False, f"Kilix opened the tab, but {vm.name}'s QMP socket did "
                       f"not answer within {self.launch_wait_seconds:g}s.",
                command)
        finally:
            self._release_launch(vm)

    def control(self, vm: VmDefinition, action: str) -> OperationResult:
        if action not in QMP_ACTIONS:
            raise ValueError(f"unsupported QMP action: {action}")
        command, label = QMP_ACTIONS[action]
        preview = self.preview(vm, action)
        try:
            qmp.execute(vm.qmp, command, timeout=max(self.qmp_timeout, 2.0),
                        allow_hangup=(command == "quit"))
        except qmp.QmpError as error:
            return OperationResult(False, f"{vm.name}: {error}", preview)
        return OperationResult(True, f"{label} {vm.name}.", preview)

    def open_ssh(self, vm: VmDefinition) -> OperationResult:
        preview = self.preview(vm, "ssh")
        if not vm.has_ssh:
            return OperationResult(False, f"{vm.name} has no SSH forward.",
                                   preview)
        if not self.rc.available():
            return OperationResult(
                False, "Open this manager inside Kilix to open an SSH tab.",
                preview)
        try:
            self.rc.launch_tab(self.ssh_argv(vm), title=f"SSH: {vm.name}",
                               keep_focus=True)
        except Exception as error:
            return OperationResult(False, f"Kilix refused the tab: {error}",
                                   preview)
        return OperationResult(
            True, f"Opened ssh -p {vm.ssh_port} {vm.ssh_user}@127.0.0.1.",
            preview)

    def disk_info(self, vm: VmDefinition) -> list[dict[str, Any]]:
        """Read-only image facts and snapshots from ``qemu-img info -U``."""
        rows: list[dict[str, Any]] = []
        for path in vm.disks:
            row: dict[str, Any] = {"path": path}
            if os.path.lexists(path) and not probe_safe(path):
                row["error"] = "not a regular file or block device; not probed"
                rows.append(row)
                continue
            try:
                done = self._runner(self.qemu_img_argv(path), 10)
            except (OSError, subprocess.SubprocessError) as error:
                row["error"] = str(error)
                rows.append(row)
                continue
            if done.returncode != 0:
                row["error"] = (done.stderr or done.stdout or
                                f"qemu-img exited {done.returncode}").strip()
                rows.append(row)
                continue
            try:
                data = json.loads(done.stdout)
            except ValueError:
                row["error"] = "qemu-img returned malformed JSON"
                rows.append(row)
                continue
            if not isinstance(data, dict):
                data = {}
            row.update({
                "format": data.get("format", ""),
                "virtual_size": data.get("virtual-size"),
                "actual_size": data.get("actual-size"),
                "backing": data.get("backing-filename", ""),
                "snapshots": [
                    {"name": str(item.get("name", "")),
                     "date": item.get("date-sec"),
                     "vm_state_size": item.get("vm-state-size")}
                    for item in data.get("snapshots") or []
                    if isinstance(item, dict)
                ],
            })
            rows.append(row)
        return rows

    def execute(self, vm: VmDefinition, action: str, *,
                size: str | None = None) -> OperationResult:
        if action == "launch":
            return self.launch(vm, size=size)
        if action == "focus":
            return self.focus(vm)
        if action == "ssh":
            return self.open_ssh(vm)
        return self.control(vm, action)
