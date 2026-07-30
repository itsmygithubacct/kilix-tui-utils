"""Safe, fixed-argv access to VBoxManage and the Kilix launcher."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .model import (
    ACTIVE_STATES,
    GuestInterface,
    HostAdapter,
    PortForward,
    VirtualMachine,
    parse_timestamp,
)


class BackendError(RuntimeError):
    """VirtualBox is unavailable or returned an unusable result."""


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    message: str
    command: tuple[str, ...]


VBoxRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
LaunchRunner = Callable[
    [Sequence[str], Mapping[str, str], float],
    subprocess.CompletedProcess[str],
]
TabFocuser = Callable[[str], int]


def _run_vbox(
    argv: Sequence[str], timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=timeout,
    )


def _run_launch(
    argv: Sequence[str], environ: Mapping[str, str], timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=timeout,
        env=dict(environ),
    )


def _focus_kilix_tab(vm_uuid: str) -> int:
    # Imported lazily so status/JSON modes remain useful without Kilix's
    # shared Python modules on sys.path.
    from kilix_tui import kitty_rc

    pane = kitty_rc.pane_with_argument(vm_uuid)
    if pane is None:
        raise kitty_rc.Unavailable(
            "the VM is running, but no Kilix tab owns its UUID")
    kitty_rc.focus_pane(pane.id)
    return pane.id


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
        value = value.replace(r"\\", "\\").replace(r"\"", '"')
    return value


_VM_LINE = re.compile(r'^("(?:\\.|[^"])*")\s+\{([^}]+)\}\s*$')


def parse_vm_list(output: str) -> list[tuple[str, str]]:
    """Parse `VBoxManage list vms` into (name, UUID) pairs."""
    machines: list[tuple[str, str]] = []
    for raw in output.splitlines():
        match = _VM_LINE.match(raw.strip())
        if not match:
            continue
        machines.append((_unquote(match.group(1)), match.group(2).lower()))
    return machines


def parse_machine_readable(output: str) -> dict[str, str]:
    """Parse `showvminfo --machinereadable` without executing its values."""
    values: dict[str, str] = {}
    for raw in output.splitlines():
        key, separator, value = raw.partition("=")
        if not separator:
            continue
        key = _unquote(key)
        if not key:
            continue
        values[key] = _unquote(value)
    return values


_GUEST_PROPERTY = re.compile(r"^(.*?)\s+=\s+'(.*)'\s+@\s+.*$")
_GUEST_NET = re.compile(
    r"^/VirtualBox/GuestInfo/Net/(\d+)/(Name|Status|V4/IP|MAC)$")


def parse_guest_properties(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in output.splitlines():
        match = _GUEST_PROPERTY.match(raw)
        if match:
            values[match.group(1).strip()] = match.group(2)
    return values


def _integer(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _host_adapters(info: Mapping[str, str]) -> tuple[HostAdapter, ...]:
    attachments = {
        "nat": "natnet",
        "bridged": "bridgeadapter",
        "hostonly": "hostonlyadapter",
        "intnet": "intnet",
        "generic": "genericdriver",
        "natnetwork": "nat-network",
    }
    adapters = []
    for index in range(1, 37):
        mode = info.get(f"nic{index}", "none")
        if mode in ("", "none", "null"):
            continue
        prefix = attachments.get(mode, "")
        attachment = info.get(f"{prefix}{index}", "") if prefix else ""
        adapters.append(HostAdapter(
            index=index,
            mode=mode,
            attachment=attachment,
            cable_connected=info.get(f"cableconnected{index}") == "on",
            mac=info.get(f"macaddress{index}", ""),
        ))
    return tuple(adapters)


def _guest_interfaces(
    properties: Mapping[str, str],
) -> tuple[GuestInterface, ...]:
    rows: dict[int, dict[str, str]] = {}
    for key, value in properties.items():
        match = _GUEST_NET.match(key)
        if not match:
            continue
        index = int(match.group(1))
        rows.setdefault(index, {})[match.group(2)] = value
    return tuple(
        GuestInterface(
            index=index,
            name=values.get("Name", ""),
            status=values.get("Status", ""),
            ipv4=values.get("V4/IP", ""),
            mac=values.get("MAC", ""),
        )
        for index, values in sorted(rows.items())
        if values.get("Name") or values.get("V4/IP")
    )


def _port_forwards(info: Mapping[str, str]) -> tuple[PortForward, ...]:
    forwards = []
    for key, value in info.items():
        if not key.startswith("Forwarding("):
            continue
        fields = value.split(",")
        if len(fields) != 6:
            continue
        forwards.append(PortForward(*fields))
    return tuple(forwards)


def _snapshots(info: Mapping[str, str]) -> tuple[str, ...]:
    rows = []
    for key, value in info.items():
        if key == "SnapshotName":
            rows.append((0, value))
        elif key.startswith("SnapshotName-"):
            rows.append((_integer(key.removeprefix("SnapshotName-")) + 1, value))
    return tuple(value for _index, value in sorted(rows))


def machine_from_info(
    fallback_name: str,
    fallback_uuid: str,
    info: Mapping[str, str],
    guest_properties: Mapping[str, str] | None = None,
) -> VirtualMachine:
    properties = guest_properties or {}
    return VirtualMachine(
        uuid=(info.get("UUID") or fallback_uuid).strip("{}").lower(),
        name=info.get("name") or fallback_name,
        state=info.get("VMState", "unknown").casefold(),
        os_type=info.get("ostype", ""),
        cpus=_integer(info.get("cpus", "")),
        memory_mb=_integer(info.get("memory", "")),
        vram_mb=_integer(info.get("vram", "")),
        state_changed=parse_timestamp(info.get("VMStateChangeTime", "")),
        session_name=info.get("SessionName", ""),
        config_file=info.get("CfgFile", ""),
        guest_additions=info.get("GuestAdditionsVersion", ""),
        host_adapters=_host_adapters(info),
        guest_interfaces=_guest_interfaces(properties),
        port_forwards=_port_forwards(info),
        snapshots=_snapshots(info),
        current_snapshot=info.get("CurrentSnapshotName", ""),
        metadata=dict(info),
    )


def _clean_error(done: subprocess.CompletedProcess[str]) -> str:
    text = (done.stderr or done.stdout or "").strip()
    if not text:
        return f"command exited {done.returncode}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.lower().startswith(("copyright", "oracle vm virtualbox")):
            return line.removeprefix("VBoxManage: error: ").strip()
    return lines[-1]


def _resolve_executable(
    override: str | None,
    command: str,
    candidates: Sequence[str] = (),
    *,
    prefer_candidates: bool = False,
) -> str:
    if override:
        expanded = os.path.abspath(os.path.expanduser(override))
        if os.access(expanded, os.X_OK):
            return expanded
        found = shutil.which(override)
        if found:
            return found
        return expanded
    expanded_candidates = [
        os.path.abspath(os.path.expanduser(candidate))
        for candidate in candidates
    ]
    if prefer_candidates:
        for candidate in expanded_candidates:
            if os.access(candidate, os.X_OK):
                return candidate
    found = shutil.which(command)
    if found:
        return found
    for expanded in expanded_candidates:
        if os.access(expanded, os.X_OK):
            return expanded
    return command


class VirtualBoxClient:
    """Discover VMs with VBoxManage and launch them through `kilix run`."""

    CONTROL_ACTIONS: dict[str, tuple[str, ...]] = {
        "pause": ("pause",),
        "resume": ("resume",),
        "shutdown": ("acpipowerbutton",),
        "save": ("savestate",),
        "reset": ("reset",),
        "poweroff": ("poweroff",),
    }

    def __init__(
        self,
        *,
        vboxmanage: str | None = None,
        virtualboxvm: str | None = None,
        kilix: str | None = None,
        runner: VBoxRunner = _run_vbox,
        launch_runner: LaunchRunner = _run_launch,
        tab_focuser: TabFocuser = _focus_kilix_tab,
        launch_wait_seconds: float = 15,
    ) -> None:
        source = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or str(
            Path.home() / "gpu_terminal")
        self.vboxmanage = _resolve_executable(
            vboxmanage or os.environ.get("VBOXMANAGE"),
            "VBoxManage",
        )
        self.virtualboxvm = _resolve_executable(
            virtualboxvm or os.environ.get("KILIX_VIRTUALBOXVM"),
            "VirtualBoxVM",
            ("/usr/lib/virtualbox/VirtualBoxVM", "/usr/bin/VirtualBoxVM"),
            prefer_candidates=True,
        )
        self.kilix = _resolve_executable(
            kilix or os.environ.get("KILIX_LAUNCHER"),
            "kilix",
            (
                str(Path.home() / ".local/bin/kilix"),
                os.path.join(source, "kilix", "kilix"),
            ),
        )
        self._runner = runner
        self._launch_runner = launch_runner
        self._tab_focuser = tab_focuser
        self._launch_wait_seconds = max(0.0, float(launch_wait_seconds))

    def _vbox(
        self, arguments: Sequence[str], timeout: float = 8,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.vboxmanage, *arguments]
        try:
            return self._runner(command, timeout)
        except FileNotFoundError as error:
            raise BackendError(
                "VBoxManage is not installed or not on PATH") from error
        except subprocess.TimeoutExpired as error:
            raise BackendError(
                f"VBoxManage did not answer within {timeout:g}s") from error
        except OSError as error:
            raise BackendError(str(error)) from error

    def version(self) -> str:
        done = self._vbox(["--version"])
        return done.stdout.strip() if done.returncode == 0 else ""

    def inventory(self) -> list[VirtualMachine]:
        listed = self._vbox(["list", "vms"])
        if listed.returncode != 0:
            raise BackendError(_clean_error(listed))
        machines = []
        for fallback_name, uuid in parse_vm_list(listed.stdout):
            shown = self._vbox(
                ["showvminfo", uuid, "--machinereadable"], timeout=10)
            if shown.returncode != 0:
                machines.append(VirtualMachine(
                    uuid=uuid,
                    name=fallback_name,
                    error=_clean_error(shown),
                ))
                continue
            info = parse_machine_readable(shown.stdout)
            properties: dict[str, str] = {}
            if info.get("VMState", "").casefold() in ACTIVE_STATES:
                guest = self._vbox(
                    ["guestproperty", "enumerate", uuid], timeout=5)
                if guest.returncode == 0:
                    properties = parse_guest_properties(guest.stdout)
            machines.append(machine_from_info(
                fallback_name, uuid, info, properties))
        return sorted(machines, key=lambda item: item.name.casefold())

    def control_command(
        self, vm: VirtualMachine, action: str,
    ) -> tuple[str, ...]:
        if action not in self.CONTROL_ACTIONS:
            raise ValueError(f"unsupported VirtualBox action: {action}")
        return (
            self.vboxmanage, "controlvm", vm.uuid,
            *self.CONTROL_ACTIONS[action],
        )

    def launch_command(
        self,
        vm: VirtualMachine,
        *,
        run_options: Sequence[str] = (),
        fullscreen: bool = False,
    ) -> tuple[str, ...]:
        virtualbox_args = [
            self.virtualboxvm,
            "--comment", vm.name,
            "--startvm", vm.uuid,
            "--no-startvm-errormsgbox",
        ]
        if fullscreen:
            virtualbox_args.append("--fullscreen")
        return (
            self.kilix, "run", "--refit-windows",
            *run_options, *virtualbox_args,
        )

    def preview(
        self,
        vm: VirtualMachine,
        action: str,
        *,
        run_options: Sequence[str] = (),
        fullscreen: bool = False,
    ) -> tuple[str, ...]:
        if action == "launch":
            return self.launch_command(
                vm, run_options=run_options, fullscreen=fullscreen)
        if action == "focus":
            return (self.kilix, "focus", f"vm:{vm.uuid}")
        return self.control_command(vm, action)

    def focus(self, vm: VirtualMachine) -> OperationResult:
        preview = (self.kilix, "focus", f"vm:{vm.uuid}")
        try:
            pane_id = self._tab_focuser(vm.uuid)
        except Exception as error:
            return OperationResult(False, str(error), preview)
        command = (self.kilix, "focus", str(pane_id))
        return OperationResult(
            True, f"Focused the {vm.name} tab.", command)

    def launch(
        self,
        vm: VirtualMachine,
        *,
        run_options: Sequence[str] = (),
        fullscreen: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> OperationResult:
        command = self.launch_command(
            vm, run_options=run_options, fullscreen=fullscreen)
        launch_env = dict(os.environ if environ is None else environ)
        needed = (
            "KITTY_WINDOW_ID", "KITTY_LISTEN_ON", "KILIX_RC_PASSWORD_FILE",
        )
        if not all(launch_env.get(name) for name in needed):
            return OperationResult(
                False,
                "Open this manager inside Kilix before launching a VM.",
                command,
            )
        # A desktop/provider tab carries this guard. The new `kilix run` call
        # must be allowed to take its normal remote-control branch and create
        # a sibling tab instead of trying to draw over this curses session.
        launch_env.pop("KILIX_IN_OVERLAY", None)
        try:
            done = self._launch_runner(command, launch_env, 15)
        except FileNotFoundError:
            return OperationResult(False, "The Kilix launcher was not found.", command)
        except subprocess.TimeoutExpired:
            return OperationResult(
                False, "Kilix did not create the VM tab within 15 seconds.", command)
        except OSError as error:
            return OperationResult(False, str(error), command)
        if done.returncode != 0:
            return OperationResult(False, _clean_error(done), command)

        # `kilix run` returns as soon as Kitty accepts the new tab. The
        # program in that tab can still fail during imports or X-server
        # startup, so do not turn "tab created" into a false VM success.
        deadline = time.monotonic() + self._launch_wait_seconds
        while True:
            try:
                shown = self._vbox(
                    ["showvminfo", vm.uuid, "--machinereadable"], timeout=5)
            except BackendError as error:
                return OperationResult(False, str(error), command)
            if shown.returncode == 0:
                state = parse_machine_readable(
                    shown.stdout).get("VMState", "").casefold()
                if state in ACTIVE_STATES:
                    return OperationResult(
                        True, f"Opened {vm.name} in a new Kilix tab.", command)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.25, remaining))

        return OperationResult(
            False,
            f"Kilix created the tab, but {vm.name} did not enter an active "
            f"VirtualBox state within {self._launch_wait_seconds:g} seconds.",
            command,
        )

    def control(self, vm: VirtualMachine, action: str) -> OperationResult:
        command = self.control_command(vm, action)
        try:
            done = self._runner(command, 120)
        except FileNotFoundError:
            return OperationResult(False, "VBoxManage was not found.", command)
        except subprocess.TimeoutExpired:
            return OperationResult(
                False, f"{action} did not finish within 120 seconds.", command)
        except OSError as error:
            return OperationResult(False, str(error), command)
        if done.returncode != 0:
            return OperationResult(False, _clean_error(done), command)
        labels = {
            "pause": "Paused",
            "resume": "Resumed",
            "shutdown": "Sent the ACPI shutdown request to",
            "save": "Saved the state of",
            "reset": "Reset",
            "poweroff": "Powered off",
        }
        return OperationResult(
            True, f"{labels[action]} {vm.name}.", command)

    def execute(
        self,
        vm: VirtualMachine,
        action: str,
        *,
        run_options: Sequence[str] = (),
        fullscreen: bool = False,
    ) -> OperationResult:
        if action == "launch":
            return self.launch(
                vm, run_options=run_options, fullscreen=fullscreen)
        if action == "focus":
            return self.focus(vm)
        return self.control(vm, action)
