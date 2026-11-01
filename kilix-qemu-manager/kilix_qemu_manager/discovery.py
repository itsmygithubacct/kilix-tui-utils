"""Proposals for the catalog. Nothing here writes a definition.

Two sources are scanned on request, and every result is only a proposal:

* ``build_vm_image.py`` VM directories, ``<session>/qemu-vms/<name>/`` with
  ``disk.qcow2`` and optional OVMF firmware. The build records neither the
  RAM, CPU count nor SSH port of a VM, so the proposed argv uses the build's
  device layout with stated defaults, to be reviewed before it is accepted;
* running ``qemu-system-*`` processes owned by this user, read from
  ``/proc/<pid>/cmdline`` and ``/proc/<pid>/cwd``.

A proposal is summarised for listing (key, origin, name, argument count). Its
full argv is shown only for the one proposal the user asks to accept, and only
in the normalised definition that acceptance would write.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import model, store
from .model import DefinitionError, VmDefinition

DEFAULT_RAM_MB = 4096
DEFAULT_CPUS = 2


@dataclass
class Proposal:
    key: str                  # plebian:<name> or pid:<pid>
    origin: str
    name: str
    qemu: str
    args: list[str]
    cwd: str
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (f"{self.key:<24} {self.origin:<16} {self.name}  "
                f"({os.path.basename(self.qemu)}, {len(self.args)} arguments)")


def plebian_vms_root(environ: dict[str, str] | None = None) -> Path:
    """build_vm_image.py's storage_dir("session") / "qemu-vms"."""
    env = os.environ if environ is None else environ
    base = Path(env.get("GPU_TERMINAL_HOME",
                        str(Path.home() / ".local" / "gpu_terminal")))
    root = Path(env.get("PLEBIAN_OS_STORAGE_HOME", str(base / "plebian-os")))
    session = Path(env.get("PLEBIAN_OS_SESSION_HOME", str(root / "session")))
    return session / "qemu-vms"


def plebian_argv(name: str, directory: Path) -> tuple[list[str], list[str]]:
    """The build's own device layout for a finished VM, minus installer bits."""
    disk = directory / "disk.qcow2"
    args = [
        "-name", name,
        "-machine", "q35,accel=kvm", "-cpu", "host",
        "-m", str(DEFAULT_RAM_MB), "-smp", str(DEFAULT_CPUS),
        "-drive", f"file={model.qemu_escape(str(disk))},format=qcow2,if=virtio",
        "-vga", "std",
        "-netdev", "user,id=net0",
        "-device", "virtio-net-pci,netdev=net0",
        "-display", "gtk",
        # The build's own pidfile, so a VM the build started is recognised.
        "-pidfile", str(directory / "qemu.pid"),
    ]
    notes = [
        f"RAM and vCPUs are not recorded by build_vm_image.py; proposed "
        f"{DEFAULT_RAM_MB} MiB and {DEFAULT_CPUS} vCPUs",
        "no SSH port is recorded; add one with --ssh USER@PORT and a "
        "matching hostfwd if the guest needs it",
    ]
    code, variables = directory / "OVMF_CODE.fd", directory / "OVMF_VARS.fd"
    if code.is_file() and variables.is_file():
        args += [
            "-drive", "if=pflash,format=raw,unit=0,readonly=on,file="
                      + model.qemu_escape(str(code)),
            "-drive", "if=pflash,format=raw,unit=1,file="
                      + model.qemu_escape(str(variables)),
        ]
        notes.append("EFI firmware copies from the VM directory are attached")
    return args, notes


def scan_plebian(root: Path | None = None) -> list[Proposal]:
    base = root or plebian_vms_root()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out: list[Proposal] = []
    for name in names:
        directory = base / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        if not (directory / "disk.qcow2").is_file():
            continue
        args, notes = plebian_argv(name, directory)
        out.append(Proposal(f"plebian:{name}", "build_vm_image", name,
                            "qemu-system-x86_64", args, str(directory), notes))
    return out


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def scan_processes(proc_root: str = "/proc",
                   known: list[VmDefinition] = ()) -> list[Proposal]:
    """Running qemu-system-* processes of this user not already catalogued."""
    uid = os.getuid()
    known_paths = {vm.pidfile for vm in known} | {
        model.qmp_spec(vm.qmp) for vm in known}
    out: list[Proposal] = []
    try:
        entries = sorted((name for name in os.listdir(proc_root)
                          if name.isdigit()), key=int)
    except OSError:
        return out
    for name in entries:
        directory = os.path.join(proc_root, name)
        try:
            if os.stat(directory).st_uid != uid:
                continue
        except OSError:
            continue
        argv = [part.decode("utf-8", "replace")
                for part in _read(os.path.join(directory, "cmdline")).split(b"\0")
                if part]
        if not argv or not model.BINARY_PATTERN.fullmatch(
                os.path.basename(argv[0])):
            continue
        if known_paths.intersection(argv):
            continue
        try:
            cwd = os.readlink(os.path.join(directory, "cwd"))
        except OSError:
            cwd = ""
        label = ""
        try:
            for item in model.parse_options(argv[1:]):
                if isinstance(item, model.Option) and item.name == "name":
                    label = model.split_keys(item.value or "")[0]
                    label = label.removeprefix("guest=")
                    break
        except DefinitionError:
            pass
        out.append(Proposal(f"pid:{name}", "running process",
                            label or f"qemu-{name}", argv[0], argv[1:], cwd,
                            ["the process keeps running; this only records "
                             "how to start it again"]))
    return out


def definition_from(
    proposal_name: str,
    qemu: str,
    args: list[str],
    *,
    cwd: str,
    vm_id: str | None = None,
    ssh: tuple[int, str] | None = None,
    notes: str = "",
    catalog: list[VmDefinition] | None = None,
) -> tuple[VmDefinition, list[str]]:
    """Normalise an argv into a definition; return it with the change notes.

    The other catalogued VMs (``catalog``, by default the stored one) own
    their pidfiles and QMP sockets; normalise never lets this VM keep them.
    """
    ident = vm_id or model.slug(proposal_name)
    if catalog is None:
        catalog, _problems = store.load_all()
    claimed = store.claimed_paths([vm for vm in catalog if vm.id != ident])
    normal = model.normalise(ident, qemu, args, cwd=cwd or os.getcwd(),
                             claimed=claimed)
    port, user = ssh if ssh else (None, "")
    vm = VmDefinition(
        id=ident,
        name=proposal_name.strip() or normal.name or ident,
        qemu=qemu,
        argv=normal.argv,
        pidfile=normal.pidfile,
        qmp=normal.qmp,
        cwd=os.path.abspath(cwd) if cwd else "",
        ssh_port=port,
        ssh_user=user,
        notes=notes,
    )
    VmDefinition.from_json(vm.as_json())
    return vm, normal.notes
