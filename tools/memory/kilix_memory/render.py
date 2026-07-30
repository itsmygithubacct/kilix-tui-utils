"""Adaptive ANSI fallback renderer."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .collect import GIB, KIB, MIB
from .model import MemoryModel


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SPARKS = "▁▂▃▄▅▆▇█"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def display_text(value: object) -> str:
    """Make process-provided text inert and single-line for terminal output."""
    result = []
    for character in str(value):
        category = unicodedata.category(character)
        if character.isspace() or category in {"Cc", "Cf", "Cs"}:
            result.append(" ")
        else:
            result.append(character)
    return "".join(result)


def format_bytes(value: float, *, short: bool = False) -> str:
    value = max(0.0, float(value))
    units = (
        ((1024.0**4), "TiB" if not short else "T"),
        ((1024.0**3), "GiB" if not short else "G"),
        ((1024.0**2), "MiB" if not short else "M"),
        (1024.0, "KiB" if not short else "K"),
    )
    for divisor, suffix in units:
        if value >= divisor:
            number = value / divisor
            if number >= 100:
                return f"{number:.0f}{suffix}"
            if number >= 10:
                return f"{number:.1f}{suffix}"
            return f"{number:.2f}{suffix}"
    return f"{value:.0f}B"


def format_rate(value: float, *, unit: str = "B") -> str:
    if unit == "B":
        return f"{format_bytes(value, short=True)}/s"
    if value >= 1000:
        return f"{value / 1000.0:.1f}k/s"
    return f"{value:.1f}/s"


def sparkline(values: list[float], width: int, *, floor: float = 0.0) -> str:
    if width <= 0:
        return ""
    if not values:
        return " " * width
    source = values[-width:]
    high = max(max(source), floor + 0.001)
    span = high - floor
    chars = []
    for value in source:
        ratio = max(0.0, min(1.0, (value - floor) / span))
        chars.append(SPARKS[min(len(SPARKS) - 1, int(ratio * len(SPARKS)))])
    return "".join(chars).rjust(width)


def meter(ratio: float, width: int) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = min(width, int(round(width * ratio)))
    return "█" * filled + "░" * (width - filled)


@dataclass(slots=True)
class FrameOptions:
    interval: float = 1.0
    paused: bool = False
    sort_mode: str = "rss"
    scroll: int = 0
    help_visible: bool = False
    notice: str = ""


class Renderer:
    is_graphical = False

    def __init__(self, color: bool = True) -> None:
        self.color = color

    def _style(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def _line(self, text: str, width: int) -> str:
        visible = strip_ansi(text)
        if len(visible) > width:
            excess = len(visible) - width
            # Keep styling intact in normal layouts; this fallback applies
            # only to an unexpectedly narrow caller.
            plain = visible[: max(0, width - 1)] + ("…" if width else "")
            return plain
        return text + " " * (width - len(visible))

    def _shell(
        self,
        width: int,
        *,
        active: int,
        summary: str,
        summary_alert: bool = False,
    ) -> list[str]:
        identity = self._style(" KILIX TUI", "1;97")
        title = self._style("Memory ", "2;37")
        gap = max(1, width - len(strip_ansi(identity)) - len(strip_ansi(title)))
        navigation = []
        for index, label in enumerate(("Monitor", "Help")):
            item = f"{'▶' if index == active else ' '}{index + 1} {label} "
            navigation.append(self._style(
                item, "1;97;44" if index == active else "2;37"))
        return [
            self._line(identity + " " * gap + title, width),
            self._line(" " + "".join(navigation), width),
            self._line(self._style("─" * max(0, width - 1), "2;37"), width),
            self._line(
                " " + self._style(
                    display_text(summary), "91" if summary_alert else "2;37"),
                width,
            ),
        ]

    def _help(self, width: int, height: int) -> str:
        entries = [
            ("q / Esc", "quit and restore the terminal"),
            ("Space", "pause or resume sampling"),
            ("r", "reset graph/rate history"),
            ("s", "cycle process sorting: RSS, PID, name, user"),
            ("↑/↓ j/k", "scroll the process table"),
            ("PgUp/PgDn", "scroll by a page"),
            ("h / ?", "close this help"),
        ]
        lines = self._shell(
            width, active=1,
            summary="HELP · Keyboard reference · monitoring only",
        )
        for key, description in entries:
            if len(lines) >= height - 2:
                break
            lines.append(self._line(f"  {key:<12} {description}", width))
        if len(lines) < height - 1:
            lines.append(self._line(
                "  Monitoring only: no process-control or reclaim actions.",
                width,
            ))
        while len(lines) < height - 1:
            lines.append(" " * width)
        lines.append(self._line(
            self._style(" 1 monitor · h/? close help · q quit ", "2;37"),
            width,
        ))
        return "\n".join(lines[:height])

    def render(
        self,
        model: MemoryModel,
        width: int,
        height: int,
        options: FrameOptions,
    ) -> str:
        width = max(40, width)
        height = max(12, height)
        if options.help_visible:
            return self._help(width, height)
        snapshot = model.current
        if snapshot is None:
            lines = self._shell(
                width, active=0, summary="Waiting for a memory sample",
                summary_alert=True,
            )
            while len(lines) < height - 1:
                lines.append(" " * width)
            lines.append(self._line(
                self._style(" q quit · h help ", "2;37"), width))
            return "\n".join(lines[:height])
        memory = snapshot.memory
        pressure = snapshot.pressure
        rates = model.rates
        status = "PAUSED" if options.paused else f"LIVE {options.interval:.1f}s"
        lines = self._shell(
            width,
            active=0,
            summary=(
                f"{display_text(snapshot.hostname)} · {status} · "
                f"{snapshot.timestamp.strftime('%H:%M:%S')}"
            ),
            summary_alert=bool(options.notice),
        )

        # The main Kilix palette is blue, red, white, and grey.  Red is the
        # only warning colour; ordinary measurements stay blue.
        used_color = "91" if memory.used_percent >= 90 else "94"
        used = self._style(
            f"{format_bytes(memory.used)} / {format_bytes(memory.total)} "
            f"({memory.used_percent:4.1f}%)",
            used_color,
        )
        available = self._style(
            f"{format_bytes(memory.available)} "
            f"({memory.available_percent:4.1f}%)",
            "94",
        )
        swap = (
            f"{format_bytes(memory.swap_used)} / {format_bytes(memory.swap_total)} "
            f"({memory.swap_percent:4.1f}%)"
            if memory.swap_total
            else "not configured"
        )
        details = [
            self._line(f" Used {used}   Available {available}", width),
            self._line(f" Swap {swap}", width),
        ]

        bar_width = max(10, width - 22)
        used_bar = meter(memory.used_percent / 100.0, bar_width)
        details.append(
            self._line(
                f" RAM  {self._style(used_bar, used_color)} "
                f"{memory.used_percent:5.1f}%",
                width,
            )
        )
        composition = "  ".join(
            f"{name} {format_bytes(value, short=True)}"
            for name, value in memory.composition
        )
        details.append(self._line(f" {composition}", width))

        pressure_text = (
            f"some {pressure.some.avg10:.2f}%  full {pressure.full.avg10:.2f}%"
            if pressure.supported
            else "unsupported"
        )
        activity = (
            f"Pressure {pressure_text}   "
            f"MajFault {format_rate(rates.major_faults_per_second, unit='events')}   "
            f"Swap in/out {format_rate(rates.swap_in_bytes_per_second)}/"
            f"{format_rate(rates.swap_out_bytes_per_second)}"
        )
        details.append(self._line(activity, width))

        graph_width = max(8, width - 19)
        used_history = [point.used_percent for point in model.history]
        pressure_history = [point.pressure_some for point in model.history]
        details.append(
            self._line(
                f" Used history     {self._style(sparkline(used_history, graph_width), '94')}",
                width,
            )
        )
        details.append(
            self._line(
                f" PSI some avg10  {self._style(sparkline(pressure_history, graph_width), '94')}",
                width,
            )
        )
        # Always preserve a process heading and footer at the minimum height.
        lines.extend(details[:max(0, height - len(lines) - 2)])

        rows = model.ordered_processes(options.sort_mode)
        table_space = max(0, height - len(lines) - 2)
        maximum_scroll = max(0, len(rows) - table_space)
        options.scroll = max(0, min(options.scroll, maximum_scroll))
        shown = rows[options.scroll : options.scroll + table_space]
        name_width = max(8, min(20, width // 5))
        user_width = max(6, min(12, width // 8))
        fixed = 7 + user_width + 1 + name_width + 1 + 9 + 7 + 2
        command_width = max(0, width - fixed)
        heading = (
            f" PID    {'USER':<{user_width}} {'PROCESS':<{name_width}} "
            f"{'RSS':>9} {'RAM%':>6}"
        )
        if command_width >= 8:
            heading += f"  {'COMMAND':<{command_width}}"
        lines.append(self._line(self._style(heading, "1;90"), width))
        for process in shown:
            command = display_text(process.command)
            if len(command) > command_width and command_width:
                command = command[: max(1, command_width - 1)] + "…"
            user = display_text(process.user)
            name = display_text(process.name)
            row = (
                f"{process.pid:>6} "
                f"{user[:user_width]:<{user_width}} "
                f"{name[:name_width]:<{name_width}} "
                f"{format_bytes(process.rss, short=True):>9} "
                f"{process.rss_percent(memory.total):>5.1f}%"
            )
            if command_width >= 8:
                row += f"  {command:<{command_width}}"
            lines.append(self._line(row, width))

        footer = display_text(options.notice) or (
            f"q quit · space pause · r reset · s sort [{options.sort_mode}] "
            "· ↑↓ scroll · h help"
        )
        while len(lines) < height - 1:
            lines.append(" " * width)
        lines.append(self._line(self._style(" " + footer, "2;37"), width))
        return "\n".join(lines[:height])
