"""Command-line entry point and non-interactive status views."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from kilix_tui import app

from .backend import BackendError, VirtualBoxClient
from .tui import State, handle, render


def _size(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", value.casefold()):
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT")
    return value.casefold()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="kilix-virtualbox-manager",
        description=(
            "List VirtualBox VPN machines, launch them in Kilix tabs, and "
            "control their state."
        ),
    )
    view = result.add_mutually_exclusive_group()
    view.add_argument(
        "-s", "--status", action="store_true",
        help="print the VM table without opening the TUI",
    )
    view.add_argument(
        "--json", action="store_true",
        help="print machine state as JSON",
    )
    result.add_argument(
        "--vm", metavar="NAME_OR_UUID",
        help="preselect one registered VM",
    )
    result.add_argument(
        "--size", type=_size, metavar="WIDTHxHEIGHT",
        help="pin the streamed VM tab to this pixel size",
    )
    result.add_argument(
        "--fps", type=int, choices=range(1, 121), metavar="N",
        help="Kilix stream frame rate (1-120)",
    )
    result.add_argument(
        "--fill", action="store_true",
        help="stretch the VM display to fill its Kilix pane",
    )
    result.add_argument(
        "--fullscreen", action="store_true",
        help="ask VirtualBox for fullscreen and make Kilix fill the pane",
    )
    result.add_argument(
        "--refresh-seconds", type=float, default=3.0, metavar="SECONDS",
        help="automatic status interval; 0 disables it (default: 3)",
    )
    result.add_argument(
        "--screenshot", metavar="PATH",
        help="write one headless TUI frame and exit",
    )
    return result


def _run_options(args: argparse.Namespace) -> tuple[str, ...]:
    options: list[str] = []
    if args.size:
        options.extend(("--size", args.size))
    if args.fps:
        options.extend(("--fps", str(args.fps)))
    if args.fill or args.fullscreen:
        options.append("--fill")
    return tuple(options)


def _select(state: State, value: str | None) -> None:
    if not value:
        return
    wanted = value.strip("{}").casefold()
    for index, vm in enumerate(state.machines):
        if vm.name.casefold() == wanted or vm.uuid.casefold() == wanted:
            state.selected = index
            return
    state.message = f"No registered VM matches {value!r}."


def _print_status(client: VirtualBoxClient, as_json: bool) -> int:
    try:
        machines = client.inventory()
    except BackendError as error:
        print(f"kilix-virtualbox-manager: {error}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(
            {"machines": [vm.as_json() for vm in machines]},
            indent=2,
        ))
        return 0
    if not machines:
        print("No registered VirtualBox machines.")
        return 0
    widths = {
        "name": max(4, min(32, max(len(vm.name) for vm in machines))),
        "state": max(5, max(len(vm.label_state) for vm in machines)),
    }
    print(
        f"{'NAME':<{widths['name']}}  "
        f"{'STATE':<{widths['state']}}  TUNNEL  UUID"
    )
    for vm in machines:
        print(
            f"{vm.name[:widths['name']]:<{widths['name']}}  "
            f"{vm.label_state:<{widths['state']}}  "
            f"{vm.tunnel_status:<12}  {vm.uuid}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.refresh_seconds < 0:
        parser().error("--refresh-seconds cannot be negative")
    client = VirtualBoxClient()
    if args.status or args.json:
        return _print_status(client, args.json)
    state = State(
        client,
        run_options=_run_options(args),
        fullscreen=args.fullscreen,
        refresh_seconds=args.refresh_seconds,
    )
    _select(state, args.vm)
    if args.screenshot:
        path = Path(args.screenshot).expanduser()
        path.write_text(app.render_to_text(render, state) + "\n", encoding="utf-8")
        return 0
    return app.run(
        render, state, handle=handle, tick_ms=500, mouse=True)
