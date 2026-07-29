from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from . import __version__
from .app import AppConfig, DashboardApp, default_log_path
from .graphics import (
    GraphicalRenderer,
    graphics_available,
    kitty_graphics_likely,
)
from .model import ThresholdConfig, ThermalModel
from .render import FrameOptions, Renderer, strip_ansi
from .sensors import DemoBackend, FanSensor, Sample, SensorBackend, TemperatureSensor
from .units import TemperatureUnit, locale_temperature_unit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kilix-temps",
        description="Live Linux temperature, fan and thermal-headroom dashboard for Kilix.",
    )
    parser.add_argument("--interval", type=float, default=0.5, help="sampling interval in seconds (default: 0.5)")
    parser.add_argument("--warning", type=float, default=80.0, help="default WARM threshold in °C")
    parser.add_argument("--hot", type=float, default=90.0, help="default HOT threshold in °C")
    parser.add_argument("--critical", type=float, default=100.0, help="default policy limit in °C")
    parser.add_argument("--history", type=int, default=180, help="samples retained per sensor (default: 180)")
    parser.add_argument("--log", metavar="PATH", type=Path, help="start CSV logging at PATH")
    parser.add_argument(
        "--log-interval",
        type=float,
        default=1.0,
        help="minimum seconds between CSV samples (default: 1.0)",
    )
    parser.add_argument("--once", action="store_true", help="print one dashboard frame and exit")
    parser.add_argument("--json", action="store_true", help="print one machine-readable sample and exit")
    parser.add_argument("--list", action="store_true", help="list discovered sensors and exit")
    parser.add_argument("--demo", action="store_true", help="use animated synthetic sensor data")
    display = parser.add_mutually_exclusive_group()
    display.add_argument(
        "--graphics",
        action="store_true",
        help="require the Kitty pixel dashboard instead of automatic detection",
    )
    display.add_argument(
        "--text",
        action="store_true",
        help="use the ANSI text dashboard even when Kitty graphics are available",
    )
    units = parser.add_mutually_exclusive_group()
    units.add_argument(
        "--fahrenheit",
        dest="temperature_unit",
        action="store_const",
        const=TemperatureUnit.FAHRENHEIT,
        help="display temperatures in Fahrenheit",
    )
    units.add_argument(
        "--celsius",
        dest="temperature_unit",
        action="store_const",
        const=TemperatureUnit.CELSIUS,
        help="display temperatures in Celsius",
    )
    parser.add_argument("--no-color", action="store_true", help="disable true-color ANSI styling")
    parser.add_argument("--no-bell", action="store_true", help="disable terminal bell on HOT/CRITICAL crossings")
    parser.add_argument("--width", type=int, help="output width for --once")
    parser.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> ThresholdConfig:
    if not 0.2 <= args.interval <= 60.0:
        parser.error("--interval must be between 0.2 and 60 seconds")
    if not 10 <= args.history <= 10_000:
        parser.error("--history must be between 10 and 10000")
    if not 0.2 <= args.log_interval <= 60.0:
        parser.error("--log-interval must be between 0.2 and 60 seconds")
    thresholds = ThresholdConfig(args.warning, args.hot, args.critical)
    try:
        thresholds.validate()
    except ValueError as error:
        parser.error(str(error))
    if args.width is not None and args.width < 40:
        parser.error("--width must be at least 40")
    return thresholds


def _backend(args: argparse.Namespace) -> SensorBackend | DemoBackend:
    return DemoBackend() if args.demo else SensorBackend(args.root)


def _model_and_sample(
    backend: SensorBackend | DemoBackend,
    thresholds: ThresholdConfig,
    history: int,
) -> tuple[ThermalModel, Sample, list[TemperatureSensor], list[FanSensor]]:
    temperatures, fans = backend.discover()
    sample = backend.sample(temperatures, fans)
    model = ThermalModel(thresholds, history)
    model.update(sample, temperatures)
    return model, sample, temperatures, fans


def _json_document(model: ThermalModel, sample: Sample, fans: list[FanSensor]) -> dict[str, object]:
    fan_by_key = {fan.key: fan for fan in fans}
    temperatures = []
    for state in model.ordered_states("source"):
        temperatures.append(
            {
                "key": state.sensor.key,
                "chip": state.sensor.chip,
                "label": state.sensor.label,
                "source": state.sensor.source,
                "celsius": state.current,
                "minimum": state.minimum,
                "maximum": state.maximum,
                "level": state.level.label,
                "warning": state.policy.warning,
                "hot": state.policy.hot,
                "limit": state.policy.critical,
                "hardware_critical": state.policy.hardware_critical,
                "headroom": state.headroom,
            }
        )
    fan_rows = []
    for key, rpm in sample.fans.items():
        fan = fan_by_key.get(key)
        fan_rows.append(
            {
                "key": key,
                "chip": fan.chip if fan else None,
                "label": fan.label if fan else key,
                "rpm": rpm,
            }
        )
    metrics = sample.metrics
    return {
        "timestamp": sample.timestamp.isoformat(timespec="milliseconds"),
        "status": model.overall_level.label,
        "temperatures": temperatures,
        "fans": fan_rows,
        "system": {
            "cpu_percent": metrics.cpu_percent,
            "load": [metrics.load_1, metrics.load_5, metrics.load_15],
            "cpu_count": metrics.cpu_count,
            "memory_percent": metrics.memory_percent,
            "uptime_seconds": metrics.uptime_seconds,
            "top_processes": [
                {
                    "name": process.name,
                    "cpu_percent": process.cpu_percent,
                    "instances": process.instances,
                }
                for process in metrics.top_processes
            ],
        },
    }


def _print_list(
    temperatures: list[TemperatureSensor],
    fans: list[FanSensor],
    sample: Sample,
    unit: TemperatureUnit,
) -> None:
    print("TEMPERATURE SENSORS")
    found_temperature = False
    for sensor in temperatures:
        value = sample.temperatures.get(sensor.key)
        if value is None:
            continue
        found_temperature = True
        hints = []
        if sensor.warning_hint is not None:
            hints.append(f"high {unit.absolute(sensor.warning_hint):.1f}{unit.symbol}")
        if sensor.critical_hint is not None:
            hints.append(
                f"crit {unit.absolute(sensor.critical_hint):.1f}{unit.symbol}"
            )
        suffix = f"  ({', '.join(hints)})" if hints else ""
        print(
            f"  {sensor.display_name:<36} "
            f"{unit.absolute(value):6.1f}{unit.symbol}{suffix}  [{sensor.source}]"
        )
    if not found_temperature:
        print("  no readable temperature sensors")
    print("FANS")
    fan_by_key = {fan.key: fan for fan in fans}
    if not sample.fans:
        print("  no readable fan tachometers")
    for key, rpm in sample.fans.items():
        fan = fan_by_key.get(key)
        name = fan.display_name if fan else key
        print(f"  {name:<36} {rpm:6d} RPM")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    temperature_unit = args.temperature_unit or locale_temperature_unit()
    thresholds = _validate(parser, args)
    backend = _backend(args)
    model, sample, temperatures, fans = _model_and_sample(
        backend, thresholds, args.history
    )

    if args.list:
        _print_list(temperatures, fans, sample, temperature_unit)
        return 0
    if args.json:
        json.dump(_json_document(model, sample, fans), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.once:
        terminal = shutil.get_terminal_size((110, 30))
        width = args.width or terminal.columns
        height = max(22, min(50, len(model.active_states) + 12))
        color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
        frame = Renderer(color).render(
            model,
            sample,
            fans,
            width,
            height,
            FrameOptions(
                interval=args.interval,
                temperature_unit=temperature_unit,
            ),
        )
        sys.stdout.write(frame if color else strip_ansi(frame))
        sys.stdout.write("\n")
        return 0

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive mode requires a terminal; use --once or --json")

    use_graphics = args.graphics or (not args.text and kitty_graphics_likely())
    if use_graphics:
        available, reason = graphics_available()
        if not available and args.graphics:
            parser.error(f"graphical mode is unavailable: {reason}")
        if not available:
            use_graphics = False

    color = not args.no_color and "NO_COLOR" not in os.environ
    log_path = args.log.expanduser() if args.log else default_log_path()
    app = DashboardApp(
        backend=backend,
        model=ThermalModel(thresholds, args.history),
        renderer=GraphicalRenderer() if use_graphics else Renderer(color),
        config=AppConfig(
            interval=args.interval,
            no_bell=args.no_bell,
            initial_log_path=log_path,
            start_logging=args.log is not None,
            log_interval=args.log_interval,
            temperature_unit=temperature_unit,
        ),
    )
    return app.run()
