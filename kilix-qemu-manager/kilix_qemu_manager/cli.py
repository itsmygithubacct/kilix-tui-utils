"""Command-line entry point, catalog commands and non-interactive views."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from kilix_tui import app

from . import desktop_entry, discovery, model, store
from .backend import QemuManager
from .model import DefinitionError
from .tui import State, handle, render

PROG = "kilix-qemu-manager"


def _size(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", value.casefold()):
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT")
    return value.casefold()


def _ssh(value: str) -> tuple[int, str]:
    try:
        return model.parse_ssh(value)
    except DefinitionError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Keep a catalog of QEMU VMs, launch them in Kilix tabs, focus "
            "their tabs, and control them over QMP. With no command, opens "
            "the interactive picker."),
        epilog=(
            "import takes the QEMU command after --, for example: "
            f"{PROG} import Scratch -- qemu-system-x86_64 -m 256 -nodefaults"),
    )
    view = result.add_mutually_exclusive_group()
    view.add_argument("-s", "--status", action="store_true",
                      help="print the VM table with live state and exit")
    view.add_argument("--json", action="store_true",
                      help="print live state as JSON and exit")
    view.add_argument("--vm", metavar="NAME",
                      help="launch NAME in a Kilix tab, or focus its tab when "
                           "it is already running")
    view.add_argument("--screenshot", metavar="PATH",
                      help="write one headless TUI frame as text and exit")
    result.add_argument("--size", type=_size, metavar="WIDTHxHEIGHT",
                        help="pass --size to kilix run for launched VMs")
    result.add_argument("--refresh-seconds", type=float, default=3.0,
                        metavar="SECONDS",
                        help="automatic status interval; 0 disables it")
    commands = result.add_subparsers(dest="command", metavar="COMMAND")

    listing = commands.add_parser("list", help="list the catalog")
    listing.add_argument("--json", action="store_true", dest="as_json",
                         help="print every definition, argv included")

    imported = commands.add_parser(
        "import", help="add a VM from a QEMU command given after --")
    imported.add_argument("name", help="display name")
    _definition_options(imported)

    found = commands.add_parser(
        "discover", help="propose VMs; nothing is added without --accept")
    found.add_argument("--accept", metavar="KEY",
                       help="adopt one proposal (plebian:NAME or pid:PID)")
    found.add_argument("--as", dest="name", metavar="NAME",
                       help="display name for the accepted VM")
    found.add_argument("--no-processes", action="store_true",
                       help="skip running qemu-system processes")
    found.add_argument("--no-plebian", action="store_true",
                       help="skip build_vm_image.py qemu-vms directories")
    _definition_options(found, with_cwd=False)

    for verb, text in (("favorite", "mark a VM as a favorite"),
                       ("unfavorite", "remove a VM's favorite mark")):
        commands.add_parser(verb, help=text).add_argument("name")
    removed = commands.add_parser(
        "remove", help="delete a definition (never a disk image)")
    removed.add_argument("name")

    return result


def _install_desktop_entry(words: list[str]) -> int:
    """install.sh's hook: write the Kilix 95 Start-menu entry."""
    hook = argparse.ArgumentParser(prog=f"{PROG} install-desktop-entry")
    hook.add_argument("--command", required=True,
                      help="absolute path of the installed launcher")
    hook.add_argument("--applications-dir", default="")
    args = hook.parse_args(words)
    try:
        path, changed = desktop_entry.install(args.command,
                                              args.applications_dir)
    except (OSError, ValueError) as error:
        print(f"{PROG}: {error}", file=sys.stderr)
        return 1
    print(("wrote " if changed else "unchanged ") + path)
    return 0


def _definition_options(command: argparse.ArgumentParser, *,
                        with_cwd: bool = True) -> None:
    command.add_argument("--id", metavar="ID",
                         help="catalog id (default: derived from the name)")
    command.add_argument("--ssh", type=_ssh, metavar="USER@PORT",
                         help="a host port forwarded to the guest's sshd")
    command.add_argument("--notes", default="", help="free-form notes")
    if with_cwd:
        command.add_argument(
            "--cwd", metavar="DIR",
            help="directory relative paths resolve against (default: here)")
    command.add_argument("--dry-run", action="store_true",
                         help="show the normalised definition; write nothing")


def _print_definition(vm, notes: list[str], *, written: str = "") -> None:
    print(json.dumps(vm.as_json(), indent=2))
    for note in notes:
        print(f"note: {note}")
    print(f"wrote {written}" if written else "dry run: nothing was written")


def _add(vm, notes: list[str], dry_run: bool) -> int:
    if dry_run:
        _print_definition(vm, notes)
        return 0
    try:
        path = store.save(vm)
    except (DefinitionError, OSError) as error:
        print(f"{PROG}: {error}", file=sys.stderr)
        return 1
    _print_definition(vm, notes, written=path)
    return 0


def _import(args: argparse.Namespace, command: list[str]) -> int:
    if not command:
        print(f"{PROG}: import needs the QEMU command after --", file=sys.stderr)
        return 2
    try:
        vm, notes = discovery.definition_from(
            args.name, command[0], command[1:],
            cwd=os.path.abspath(args.cwd or os.getcwd()),
            vm_id=args.id, ssh=args.ssh, notes=args.notes)
    except DefinitionError as error:
        print(f"{PROG}: refused: {error}", file=sys.stderr)
        return 1
    return _add(vm, notes, args.dry_run)


def _discover(args: argparse.Namespace, manager: QemuManager) -> int:
    known, _problems = store.load_all()
    proposals: list[discovery.Proposal] = []
    if not args.no_plebian:
        proposals += discovery.scan_plebian()
    if not args.no_processes:
        proposals += discovery.scan_processes(manager.proc_root, known)
    if not args.accept:
        if not proposals:
            print("No QEMU VMs to propose.")
            return 0
        print("Proposals (nothing has been added):")
        for proposal in proposals:
            print(f"  {proposal.summary}")
        print(f"Adopt one with: {PROG} discover --accept KEY [--as NAME]")
        return 0
    chosen = next((item for item in proposals if item.key == args.accept), None)
    if chosen is None:
        print(f"{PROG}: no proposal {args.accept!r}", file=sys.stderr)
        return 1
    try:
        vm, notes = discovery.definition_from(
            args.name or chosen.name, chosen.qemu, chosen.args,
            cwd=chosen.cwd, vm_id=args.id, ssh=args.ssh, notes=args.notes)
    except DefinitionError as error:
        print(f"{PROG}: refused: {error}", file=sys.stderr)
        return 1
    return _add(vm, chosen.notes + notes, args.dry_run)


def _lookup(name: str):
    vm = store.find(name)
    if vm is None:
        print(f"{PROG}: no VM matches {name!r}", file=sys.stderr)
    return vm


def _status_table(manager: QemuManager, as_json: bool) -> int:
    machines, problems, tree_error = manager.inventory()
    if as_json:
        print(json.dumps({
            "machines": [status.as_json() for status in machines],
            "problems": problems,
            "kilix": tree_error or "ok",
        }, indent=2))
        return 0
    for problem in problems:
        print(f"{PROG}: skipped {problem}", file=sys.stderr)
    if not machines:
        print("No QEMU VMs in the catalog.")
        return 0
    width = max(4, min(32, max(len(status.vm.name) for status in machines)))
    print(f"   {'NAME':<{width}}  {'STATE':<20}  TAB    ID")
    for status in machines:
        star = "★" if status.favorite else " "
        tab = str(status.pane_id) if status.pane_id else "-"
        print(f" {star} {status.vm.name[:width]:<{width}}  "
              f"{status.label:<20}  {tab:<6} {status.vm.id}")
    return 0


def _list(as_json: bool) -> int:
    machines, problems = store.load_all()
    machines = store.ordered(machines)
    record = store.load_state()
    if as_json:
        print(json.dumps({"machines": [vm.as_json() for vm in machines],
                          "favorites": record["favorites"],
                          "recents": record["recents"],
                          "problems": problems}, indent=2))
        return 0
    if not machines:
        print("No QEMU VMs in the catalog.")
        return 0
    last = {row["id"]: row["at"] for row in record["recents"]}
    for vm in machines:
        star = "★" if vm.id in record["favorites"] else " "
        ssh = f"ssh {vm.ssh_user}@:{vm.ssh_port}" if vm.has_ssh else ""
        print(f" {star} {vm.id:<20} {vm.name:<24} {last.get(vm.id, ''):<26}"
              f" {ssh}".rstrip())
    return 0


def _launch_or_focus(manager: QemuManager, name: str, size: str | None) -> int:
    vm = _lookup(name)
    if vm is None:
        return 1
    tree, _problem = manager.tree()
    status = manager.status(vm, tree)
    action = "focus" if status.live and status.pane_id else "launch"
    result = manager.execute(vm, action, size=size)
    print(result.message, file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    words = list(sys.argv[1:] if argv is None else argv)
    if words[:1] == ["install-desktop-entry"]:
        return _install_desktop_entry(words[1:])
    head, tail = words, []
    if "--" in words:
        index = words.index("--")
        head, tail = words[:index], words[index + 1:]
    args = parser().parse_args(head)
    if args.refresh_seconds < 0:
        parser().error("--refresh-seconds cannot be negative")
    if tail and args.command != "import":
        parser().error("only import takes a command after --")
    if args.command == "import":
        return _import(args, tail)
    if args.command in ("favorite", "unfavorite"):
        vm = _lookup(args.name)
        if vm is None:
            return 1
        changed = store.set_favorite(vm.id, args.command == "favorite")
        state = "a favorite" if args.command == "favorite" else "not a favorite"
        print(f"{vm.name} is {'now' if changed else 'already'} {state}.")
        return 0
    if args.command == "list":
        return _list(args.as_json)

    manager = QemuManager()
    if args.command == "discover":
        return _discover(args, manager)
    if args.command == "remove":
        vm = _lookup(args.name)
        if vm is None:
            return 1
        tree, _problem = manager.tree()
        if manager.status(vm, tree).live:
            print(f"{PROG}: {vm.name} is running; stop it before removing its "
                  "definition", file=sys.stderr)
            return 1
        path = store.remove(vm.id)
        print(f"Removed the definition {path}; no disk image was touched.")
        return 0
    if args.status or args.json:
        return _status_table(manager, args.json)
    if args.vm:
        return _launch_or_focus(manager, args.vm, args.size)
    state = State(manager, size=args.size, refresh_seconds=args.refresh_seconds)
    if args.screenshot:
        Path(args.screenshot).expanduser().write_text(
            app.render_to_text(render, state) + "\n", encoding="utf-8")
        return 0
    return app.run(render, state, handle=handle, tick_ms=500, mouse=True)
