from __future__ import annotations

from dataclasses import dataclass
import re
import socket

from .model import Level, SensorState, ThermalModel
from .sensors import FanSensor, Sample
from .units import TemperatureUnit


ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SPARKS = "▁▂▃▄▅▆▇█"

TANGO_BG = (29, 31, 33)
TANGO_FG = (211, 215, 207)
TANGO_BLUE = (52, 101, 164)
TANGO_BRIGHT_BLUE = (114, 159, 207)
TANGO_GREEN = (138, 226, 52)
TANGO_YELLOW = (252, 233, 79)
TANGO_ORANGE = (245, 121, 0)
TANGO_RED = (239, 41, 41)
TANGO_CYAN = (52, 226, 226)
TANGO_DIM = (136, 138, 133)
WHITE = (238, 238, 236)
BLACK = (29, 31, 33)


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def visible_len(value: str) -> int:
    return len(strip_ansi(value))


def _clip_ansi(value: str, width: int) -> str:
    if width <= 0:
        return ""
    output: list[str] = []
    visible = 0
    position = 0
    for match in ANSI_RE.finditer(value):
        text = value[position : match.start()]
        take = min(len(text), width - visible)
        output.append(text[:take])
        visible += take
        if visible >= width:
            return "".join(output) + "\x1b[0m"
        output.append(match.group(0))
        position = match.end()
    if visible < width:
        output.append(value[position : position + width - visible])
    return "".join(output)


def fit(value: str, width: int, *, align: str = "left") -> str:
    length = visible_len(value)
    if length > width:
        if width <= 1:
            return _clip_ansi(value, width)
        value = _clip_ansi(value, width - 1) + "…"
        length = width
    padding = max(0, width - length)
    if align == "right":
        return " " * padding + value
    if align == "center":
        left = padding // 2
        return " " * left + value + " " * (padding - left)
    return value + " " * padding


def align_lr(left: str, right: str, width: int) -> str:
    if visible_len(left) + visible_len(right) + 1 > width:
        left_width = max(1, width - visible_len(right) - 1)
        left = fit(left, left_width).rstrip()
    gap = max(1, width - visible_len(left) - visible_len(right))
    return fit(left + " " * gap + right, width)


class Theme:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def paint(
        self,
        value: str,
        *,
        foreground: tuple[int, int, int] | None = None,
        background: tuple[int, int, int] | None = None,
        bold: bool = False,
        dim: bool = False,
    ) -> str:
        if not self.enabled:
            return value
        codes: list[str] = []
        if bold:
            codes.append("1")
        if dim:
            codes.append("2")
        if foreground is not None:
            codes.append(f"38;2;{foreground[0]};{foreground[1]};{foreground[2]}")
        if background is not None:
            codes.append(f"48;2;{background[0]};{background[1]};{background[2]}")
        if not codes:
            return value
        return f"\x1b[{';'.join(codes)}m{value}\x1b[0m"

    def level_color(self, level: Level) -> tuple[int, int, int]:
        return {
            Level.NORMAL: TANGO_GREEN,
            Level.WARM: TANGO_YELLOW,
            Level.HOT: TANGO_ORANGE,
            Level.CRITICAL: TANGO_RED,
        }[level]

    def level_background(self, level: Level) -> tuple[int, int, int]:
        return {
            Level.NORMAL: (34, 68, 20),
            Level.WARM: (92, 76, 0),
            Level.HOT: (135, 58, 0),
            Level.CRITICAL: (145, 0, 0),
        }[level]


def sparkline(values: list[float], width: int, low: float, high: float) -> str:
    if width <= 0:
        return ""
    if not values:
        return " " * width
    if len(values) > width:
        start = len(values) - width
        values = values[start:]
    span = max(1.0, high - low)
    chars: list[str] = []
    for value in values:
        normalized = max(0.0, min(1.0, (value - low) / span))
        chars.append(SPARKS[min(len(SPARKS) - 1, int(normalized * len(SPARKS)))])
    return " " * (width - len(chars)) + "".join(chars)


def gauge(value: float, maximum: float, width: int) -> str:
    if width <= 2:
        return ""
    inner = width - 2
    ratio = max(0.0, min(1.0, value / max(1.0, maximum)))
    filled = int(round(inner * ratio))
    return "[" + "█" * filled + "·" * (inner - filled) + "]"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


@dataclass(slots=True)
class FrameOptions:
    paused: bool = False
    interval: float = 0.5
    logging: bool = False
    log_path: str = ""
    scroll: int = 0
    sort_mode: str = "risk"
    help_visible: bool = False
    notice: str = ""
    temperature_unit: TemperatureUnit = TemperatureUnit.FAHRENHEIT


class Renderer:
    def __init__(self, color: bool = True) -> None:
        self.theme = Theme(color)
        self.hostname = socket.gethostname()

    def _border(self, text: str) -> str:
        return self.theme.paint(text, foreground=TANGO_BRIGHT_BLUE)

    def _box_top(self, title: str, width: int) -> str:
        title_width = max(1, width - 4)
        clipped_title = fit(title, title_width).rstrip()
        title_text = f" {clipped_title} "
        remaining = max(0, width - 2 - visible_len(title_text))
        return self._border("┌" + title_text + "─" * remaining + "┐")

    def _box_bottom(self, width: int) -> str:
        return self._border("└" + "─" * max(0, width - 2) + "┘")

    def _box_line(self, content: str, width: int) -> str:
        return self._border("│") + fit(content, max(0, width - 2)) + self._border("│")

    def _headers(self, model: ThermalModel, sample: Sample, width: int, options: FrameOptions) -> list[str]:
        clock = sample.timestamp.strftime("%Y-%m-%d  %H:%M:%S")
        title = " KILIX TEMPS  //  THERMAL WATCH "
        host = f"{self.hostname}  {clock} "
        first = align_lr(title, host, width)
        first = self.theme.paint(first, foreground=WHITE, background=TANGO_BLUE, bold=True)

        level = model.overall_level
        risk = model.most_at_risk
        if risk is None or risk.current is None:
            summary = " NO SENSOR DATA  //  waiting for readable Linux thermal sensors"
        else:
            unit = options.temperature_unit
            current = unit.absolute(risk.current)
            critical = unit.absolute(risk.policy.critical)
            headroom = unit.delta(risk.headroom or 0.0)
            trend = risk.trend_per_minute
            trend_text = ""
            if trend is not None:
                trend_text = f"  trend {unit.delta(trend):+.1f}{unit.symbol}/min"
            summary = (
                f" {level.label:<8} // {risk.sensor.display_name}  {current:5.1f}{unit.symbol}"
                f"  limit {critical:.1f}{unit.symbol}  headroom {headroom:.1f}°{trend_text}"
            )
        mode = "PAUSED" if options.paused else f"LIVE {options.interval:.1f}s"
        second = align_lr(summary, f"{mode} ", width)
        second = self.theme.paint(
            second,
            foreground=WHITE if level >= Level.HOT else TANGO_FG,
            background=self.theme.level_background(level),
            bold=level >= Level.HOT,
        )
        return [first, second]

    def _column_header(self, width: int) -> tuple[str, str]:
        inner = width - 2
        if width >= 118:
            history_width = max(8, inner - 30 - 7 * 6 - 9 - 8)
            header = (
                f"{'SENSOR':<30} {'NOW':>7} {'MIN':>7} {'PEAK':>7} "
                f"{'WARN':>7} {'LIMIT':>7} {'ROOM':>7} {'TREND':>9} "
                f"{'HISTORY':<{history_width}}"
            )
            return "wide", fit(header, inner)
        if width >= 82:
            history_width = 13
            name_width = max(18, inner - (7 * 5 + history_width + 6))
            header = (
                f"{'SENSOR':<{name_width}} {'NOW':>7} {'MIN':>7} {'PEAK':>7} "
                f"{'LIMIT':>7} {'ROOM':>7} {'HISTORY':<{history_width}}"
            )
            return f"medium:{name_width}:{history_width}", fit(header, inner)
        name_width = max(4, inner - 6 - 6 - 8 - 3)
        header = f"{'SENSOR':<{name_width}} {'NOW':>6} {'LIMIT':>6} {'STATE':>8}"
        return f"narrow:{name_width}", fit(header, inner)

    def _sensor_row(
        self,
        state: SensorState,
        layout: str,
        width: int,
        unit: TemperatureUnit,
    ) -> str:
        assert state.current is not None
        minimum_value = state.minimum if state.minimum is not None else state.current
        maximum_value = state.maximum if state.maximum is not None else state.current
        current = f"{unit.absolute(state.current):5.1f}°"
        minimum = f"{unit.absolute(minimum_value):5.1f}°"
        maximum = f"{unit.absolute(maximum_value):5.1f}°"
        warning = f"{unit.absolute(state.policy.warning):5.1f}°"
        critical = f"{unit.absolute(state.policy.critical):5.1f}°"
        headroom = f"{unit.delta(state.headroom or 0.0):+5.1f}°"
        trend_value = state.trend_per_minute
        trend = (
            "   --/min"
            if trend_value is None
            else f"{unit.delta(trend_value):+6.1f}/m"
        )
        color = self.theme.level_color(state.level)
        current = self.theme.paint(current, foreground=color, bold=state.level >= Level.HOT)
        name = self.theme.paint(state.sensor.display_name, foreground=color)
        inner = width - 2

        if layout == "wide":
            history_width = max(8, inner - 30 - 7 * 6 - 9 - 8)
            history = sparkline(state.values, history_width, 30.0, state.policy.critical)
            history = self.theme.paint(history, foreground=color)
            row = (
                f"{fit(name, 30)} {fit(current, 7, align='right')} {minimum:>7} {maximum:>7} "
                f"{warning:>7} {critical:>7} {headroom:>7} {trend:>9} {history}"
            )
            return fit(row, inner)
        if layout.startswith("medium:"):
            _, name_text, history_text = layout.split(":")
            name_width = int(name_text)
            history_width = int(history_text)
            history = sparkline(state.values, history_width, 30.0, state.policy.critical)
            history = self.theme.paint(history, foreground=color)
            row = (
                f"{fit(name, name_width)} {fit(current, 7, align='right')} {minimum:>7} "
                f"{maximum:>7} {critical:>7} {headroom:>7} {history}"
            )
            return fit(row, inner)
        name_width = int(layout.split(":")[1])
        state_text = self.theme.paint(f"{state.level.label:>8}", foreground=color, bold=True)
        row = (
            f"{fit(name, name_width)} {fit(current, 6, align='right')} "
            f"{fit(critical, 6, align='right')} {state_text}"
        )
        return fit(row, inner)

    def _sensor_box(
        self,
        model: ThermalModel,
        width: int,
        rows: int,
        options: FrameOptions,
    ) -> list[str]:
        states = model.ordered_states(options.sort_mode)
        maximum_scroll = max(0, len(states) - rows)
        scroll = max(0, min(options.scroll, maximum_scroll))
        shown = states[scroll : scroll + rows]
        start = scroll + 1 if states else 0
        end = scroll + len(shown)
        title = (
            f"THERMAL SENSORS  //  {start}-{end} of {len(states)}"
            f"  //  {options.temperature_unit.symbol}  //  sort: {options.sort_mode}"
        )
        lines = [self._box_top(title, width)]
        layout, header = self._column_header(width)
        lines.append(self._box_line(self.theme.paint(header, foreground=TANGO_DIM, bold=True), width))
        for state in shown:
            lines.append(
                self._box_line(
                    self._sensor_row(
                        state, layout, width, options.temperature_unit
                    ),
                    width,
                )
            )
        missing_rows = rows - len(shown)
        for index in range(missing_rows):
            message = "No readable sensors" if not states and index == 0 else ""
            lines.append(self._box_line(message, width))
        lines.append(self._box_bottom(width))
        return lines

    def _cooling_box(
        self,
        sample: Sample,
        fan_specs: list[FanSensor],
        width: int,
        include_processes: bool,
    ) -> list[str]:
        metrics = sample.metrics
        cpu_value = metrics.cpu_percent
        cpu_text = "CPU warmup" if cpu_value is None else f"CPU {cpu_value:5.1f}%"
        bar_width = 12 if width >= 90 else 8
        bar_value = metrics.load_1 * 100.0 / max(1, metrics.cpu_count) if cpu_value is None else cpu_value
        cpu_bar = gauge(bar_value, 100.0, bar_width)
        load = f"load {metrics.load_1:.2f} {metrics.load_5:.2f} {metrics.load_15:.2f} / {metrics.cpu_count}t"
        memory = ""
        if metrics.memory_percent is not None and width >= 82:
            memory = f"  mem {metrics.memory_percent:.0f}%"
        fan_names = {fan.key: fan for fan in fan_specs}
        fan_parts = []
        for key, rpm in sample.fans.items():
            fan = fan_names.get(key)
            label = fan.label if fan is not None else key
            fan_parts.append(f"{label} {rpm:,} RPM")
        fan_text = "  |  ".join(fan_parts) if fan_parts else "fan telemetry unavailable"
        uptime = f"up {format_duration(metrics.uptime_seconds)}"
        content = f" {cpu_text} {cpu_bar}  {load}{memory}  |  {fan_text}  |  {uptime}"
        lines = [
            self._box_top("COOLING & LOAD", width),
            self._box_line(content, width),
        ]
        if include_processes:
            if metrics.top_processes:
                process_parts = []
                for process in metrics.top_processes:
                    instances = f" ×{process.instances}" if process.instances > 1 else ""
                    process_parts.append(
                        f"{process.name}{instances} {process.cpu_percent:.0f}%"
                    )
                processes = "  |  ".join(process_parts)
                process_line = f" heat sources  {processes}"
            else:
                process_line = " heat sources  collecting a 2-second CPU baseline…"
            lines.append(self._box_line(process_line, width))
        lines.append(self._box_bottom(width))
        return lines

    def _alerts_box(
        self,
        model: ThermalModel,
        sample: Sample,
        width: int,
        options: FrameOptions,
    ) -> list[str]:
        if model.overall_level >= Level.HOT:
            action = "ACTION: reduce or stop heavy parallel jobs; thermal headroom is low."
            action = self.theme.paint(action, foreground=TANGO_RED, bold=True)
        elif model.overall_level == Level.WARM:
            action = "Thermals are elevated; watch the trend and cooling response."
            action = self.theme.paint(action, foreground=TANGO_YELLOW)
        else:
            action = "Thermal headroom is healthy."
            action = self.theme.paint(action, foreground=TANGO_GREEN)
        latest = ""
        if model.alerts:
            alert = model.alerts[0]
            ago = max(0, int(sample.monotonic - alert.monotonic))
            value = options.temperature_unit.absolute(alert.value)
            latest = (
                f"  Last crossing: {alert.level.label} {alert.sensor_name} "
                f"{value:.1f}{options.temperature_unit.symbol}, {ago}s ago."
            )
        else:
            latest = "  No HOT/CRITICAL crossings this session."
        return [
            self._box_top("THERMAL GUARD", width),
            self._box_line(" " + action + latest, width),
            self._box_bottom(width),
        ]

    def _footer(self, width: int, options: FrameOptions) -> str:
        keys = " q quit  space pause  r reset  +/- sample  ↑↓ scroll  s sort  u unit  l log  h help "
        if width < 82:
            keys = " q quit  space pause  r reset  u unit  ↑↓ scroll  h help "
        log_state = options.notice or ("LOG ON" if options.logging else "LOG OFF")
        return self.theme.paint(
            align_lr(keys, f"{log_state} ", width),
            foreground=TANGO_FG,
            background=(46, 52, 54),
        )

    def _help(self, model: ThermalModel, sample: Sample, width: int, height: int, options: FrameOptions) -> str:
        lines = self._headers(model, sample, width, options)
        body = [
            ("q / Esc", "quit and restore the terminal"),
            ("Space", "pause or resume sampling"),
            ("r", "reset minimum, peak and graph history"),
            ("+ / -", "sample faster or slower"),
            ("Up / Down, j / k", "scroll the sensor table"),
            ("s", "toggle risk/source sorting"),
            ("u", "toggle Fahrenheit/Celsius display"),
            ("l", "toggle CSV logging in the XDG state directory"),
            ("h / ?", "close this help"),
            ("", ""),
            (
                "Alert policy",
                f"WARM {options.temperature_unit.absolute(model.thresholds.warning):.0f}{options.temperature_unit.symbol}  "
                f"HOT {options.temperature_unit.absolute(model.thresholds.hot):.0f}{options.temperature_unit.symbol}  "
                f"LIMIT {options.temperature_unit.absolute(model.thresholds.critical):.0f}{options.temperature_unit.symbol}",
            ),
            ("Driver limits", "Lower exported limits win; the dashboard never raises them."),
            ("Safety", "Monitoring only: it never kills jobs, throttles CPUs, or powers off."),
        ]
        lines.append(self._box_top("HELP", width))
        available = max(1, height - len(lines) - 2)
        for key, description in body[:available]:
            content = f" {key:<20} {description}" if key else ""
            lines.append(self._box_line(content, width))
        while len(lines) < height - 2:
            lines.append(self._box_line("", width))
        lines.append(self._box_bottom(width))
        lines.append(self._footer(width, options))
        return "\n".join(lines[:height])

    def render(
        self,
        model: ThermalModel,
        sample: Sample,
        fan_specs: list[FanSensor],
        width: int,
        height: int,
        options: FrameOptions,
    ) -> str:
        if width < 40 or height < 10:
            width = max(1, width)
            height = max(1, height)
            message = fit("Kilix Temps", width, align="center")
            requirement = fit("pane needs 40 x 10", width, align="center")
            lines = [" " * width for _ in range(height)]
            if height:
                lines[max(0, height // 2 - 1)] = self.theme.paint(
                    message, foreground=WHITE, background=TANGO_BLUE, bold=True
                )
            if height >= 2:
                lines[min(height - 1, height // 2)] = requirement
            return "\n".join(lines)
        if options.help_visible:
            return self._help(model, sample, width, height, options)

        lines = self._headers(model, sample, width, options)
        show_alerts = height >= 22
        show_processes = height >= 16
        cooling_lines = 4 if show_processes else 3
        fixed = 2 + 3 + (3 if show_alerts else 0) + cooling_lines + 1
        sensor_rows = max(1, height - fixed)
        lines.extend(self._sensor_box(model, width, sensor_rows, options))
        lines.extend(
            self._cooling_box(sample, fan_specs, width, include_processes=show_processes)
        )
        if show_alerts:
            lines.extend(self._alerts_box(model, sample, width, options))
        lines.append(self._footer(width, options))
        while len(lines) < height:
            lines.append(" " * width)
        return "\n".join(lines[:height])
