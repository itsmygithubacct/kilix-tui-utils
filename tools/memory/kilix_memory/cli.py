"""Command-line interface for Kilix Memory."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from . import __version__
from .app import AppConfig, DashboardApp
from .collect import DemoMemoryBackend, LinuxMemoryBackend, MemorySnapshot
from .graphics import (
    GraphicalRenderer,
    graphics_available,
)
from .model import MemoryModel
from .render import FrameOptions, Renderer, strip_ansi


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kilix-memory",
        description=(
            "Live Linux RAM, swap, pressure, paging, and process-memory "
            "dashboard for Kilix."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="sampling interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=240,
        help="samples retained for graphs (default: 240)",
    )
    parser.add_argument(
        "--sort",
        choices=("rss", "pid", "name", "user"),
        default="rss",
        help="initial process ordering (default: rss)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="print one ANSI/text dashboard frame and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one machine-readable snapshot and exit",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="use animated synthetic memory data",
    )
    display = parser.add_mutually_exclusive_group()
    display.add_argument(
        "--graphics",
        action="store_true",
        help="require the Kitty pixel dashboard",
    )
    display.add_argument(
        "--text",
        action="store_true",
        help="use the canonical text interface (the default)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color in text mode",
    )
    parser.add_argument(
        "--width",
        type=int,
        help="output width for --once",
    )
    parser.add_argument(
        "--height",
        type=int,
        help="output height for --once",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _validate(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if not 0.2 <= args.interval <= 60.0:
        parser.error("--interval must be between 0.2 and 60 seconds")
    if not 10 <= args.history <= 10_000:
        parser.error("--history must be between 10 and 10000")
    if args.width is not None and args.width < 40:
        parser.error("--width must be at least 40")
    if args.height is not None and args.height < 12:
        parser.error("--height must be at least 12")


def _backend(
    args: argparse.Namespace,
) -> DemoMemoryBackend | LinuxMemoryBackend:
    return DemoMemoryBackend() if args.demo else LinuxMemoryBackend(args.root)


def _json_document(
    snapshot: MemorySnapshot,
    model: MemoryModel,
) -> dict[str, object]:
    memory = snapshot.memory
    pressure = snapshot.pressure
    rates = model.rates
    return {
        "timestamp": snapshot.timestamp.isoformat(timespec="milliseconds"),
        "hostname": snapshot.hostname,
        "memory": {
            "total": memory.total,
            "used": memory.used,
            "available": memory.available,
            "free": memory.free,
            "used_percent": memory.used_percent,
            "available_percent": memory.available_percent,
            "buffers": memory.buffers,
            "cached": memory.cache_bytes,
            "active": memory.active,
            "inactive": memory.inactive,
            "anonymous": memory.anon,
            "shared": memory.shmem,
            "slab": memory.slab,
            "page_tables": memory.page_tables,
            "kernel_stack": memory.kernel_stack,
            "dirty": memory.dirty,
            "writeback": memory.writeback,
            "composition": dict(memory.composition),
        },
        "swap": {
            "total": memory.swap_total,
            "used": memory.swap_used,
            "free": memory.swap_free,
            "used_percent": memory.swap_percent,
        },
        "pressure": {
            "supported": pressure.supported,
            "some": {
                "avg10": pressure.some.avg10,
                "avg60": pressure.some.avg60,
                "avg300": pressure.some.avg300,
                "total_us": pressure.some.total_us,
            },
            "full": {
                "avg10": pressure.full.avg10,
                "avg60": pressure.full.avg60,
                "avg300": pressure.full.avg300,
                "total_us": pressure.full.total_us,
            },
        },
        "rates": {
            "faults_per_second": rates.faults_per_second,
            "major_faults_per_second": rates.major_faults_per_second,
            "swap_in_bytes_per_second": rates.swap_in_bytes_per_second,
            "swap_out_bytes_per_second": rates.swap_out_bytes_per_second,
            "scan_pages_per_second": rates.scan_pages_per_second,
            "steal_pages_per_second": rates.steal_pages_per_second,
            "oom_kills_delta": rates.oom_kills_delta,
            "alloc_stalls_per_second": rates.alloc_stalls_per_second,
            "compact_stalls_per_second": rates.compact_stalls_per_second,
        },
        "processes": [
            {
                "pid": process.pid,
                "ppid": process.ppid,
                "user": process.user,
                "name": process.name,
                "state": process.state,
                "threads": process.threads,
                "rss": process.rss,
                "rss_percent": process.rss_percent(memory.total),
                "virtual": process.virtual,
                "anonymous": process.anon,
                "file": process.file,
                "shared": process.shared,
                "command": process.command,
            }
            for process in model.ordered_processes("rss")
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    backend = _backend(args)

    if args.json or args.once:
        model = MemoryModel(args.history)
        try:
            model.update(backend.sample())
        except (OSError, ValueError) as error:
            parser.exit(1, f"kilix-memory: {error}\n")
        assert model.current is not None
    if args.json:
        json.dump(_json_document(model.current, model), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.once:
        terminal = shutil.get_terminal_size((110, 34))
        width = args.width or terminal.columns
        height = args.height or terminal.lines
        color = (
            sys.stdout.isatty()
            and not args.no_color
            and "NO_COLOR" not in os.environ
        )
        options = FrameOptions(
            interval=args.interval,
            sort_mode=args.sort,
        )
        frame = Renderer(color).render(model, width, height, options)
        sys.stdout.write(frame if color else strip_ansi(frame))
        sys.stdout.write("\n")
        return 0

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive mode requires a terminal; use --once or --json")

    use_graphics = args.graphics
    if use_graphics:
        available, reason = graphics_available()
        if not available and args.graphics:
            parser.error(f"graphical mode is unavailable: {reason}")
        if not available:
            use_graphics = False
    color = not args.no_color and "NO_COLOR" not in os.environ
    app = DashboardApp(
        backend=backend,
        model=MemoryModel(args.history),
        renderer=GraphicalRenderer() if use_graphics else Renderer(color),
        config=AppConfig(interval=args.interval),
    )
    app.options.sort_mode = args.sort
    return app.run()
