"""Responsive curses interface for the Kilix VirtualBox VPN manager."""
from __future__ import annotations

import curses
import queue
import shlex
import threading
import time
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from kilix_desk import tango
from kilix_tui import keys as keymap, shell

from .backend import BackendError, OperationResult, VirtualBoxClient
from .model import VirtualMachine


DETAIL_TABS = ("Overview", "Network", "Snapshots", "Help")


@dataclass(frozen=True)
class Action:
    ident: str
    label: str
    description: str
    confirm: bool = False
    danger: bool = False


def actions_for(vm: VirtualMachine) -> list[Action]:
    actions: list[Action] = []
    if vm.startable:
        actions.append(Action(
            "launch", "Launch in a Kilix tab",
            "Stream VirtualBoxVM through kilix run.",
        ))
    elif vm.active:
        actions.append(Action(
            "focus", "Focus its Kilix tab",
            "Switch to the tab whose command owns this VM UUID.",
        ))

    if vm.running:
        actions.append(Action("pause", "Pause", "Freeze guest execution."))
    elif vm.paused:
        actions.append(Action("resume", "Resume", "Continue guest execution."))

    if vm.active:
        actions.extend([
            Action(
                "shutdown", "Request shutdown",
                "Send the guest an ACPI power-button event.", confirm=True,
            ),
            Action(
                "save", "Save state",
                "Save RAM/device state, then stop the VM.", confirm=True,
            ),
            Action(
                "reset", "Reset",
                "Hard-reset the VM without a guest shutdown.",
                confirm=True, danger=True,
            ),
            Action(
                "poweroff", "Power off",
                "Cut VM power immediately; unsaved guest data can be lost.",
                confirm=True, danger=True,
            ),
        ])
    return actions


class State:
    """All mutable state, including one serialized background operation."""

    def __init__(
        self,
        client: VirtualBoxClient | None = None,
        *,
        run_options: Sequence[str] = (),
        fullscreen: bool = False,
        refresh_seconds: float = 3.0,
    ) -> None:
        self.client = client or VirtualBoxClient()
        self.run_options = tuple(run_options)
        self.fullscreen = fullscreen
        self.refresh_seconds = max(0.0, refresh_seconds)
        self.machines: list[VirtualMachine] = []
        self.selected = 0
        self.detail_tab = 0
        self.modal = ""
        self.action_index = 0
        self.confirm_action: Action | None = None
        self.message = ""
        self.error = ""
        self.pending = ""
        self.last_refresh = 0.0
        self.row_hits: list[tuple[int, int]] = []
        self._results: queue.SimpleQueue[OperationResult] = queue.SimpleQueue()
        self.refresh()

    @property
    def selected_vm(self) -> VirtualMachine | None:
        if not self.machines:
            return None
        self.selected = max(0, min(self.selected, len(self.machines) - 1))
        return self.machines[self.selected]

    @property
    def actions(self) -> list[Action]:
        vm = self.selected_vm
        return actions_for(vm) if vm else []

    def refresh(self) -> None:
        selected_uuid = self.selected_vm.uuid if self.selected_vm else ""
        try:
            machines = self.client.inventory()
        except BackendError as error:
            self.error = str(error)
            self.last_refresh = time.monotonic()
            return
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            self.last_refresh = time.monotonic()
            return
        self.machines = machines
        self.error = ""
        if selected_uuid:
            for index, vm in enumerate(machines):
                if vm.uuid == selected_uuid:
                    self.selected = index
                    break
            else:
                self.selected = min(self.selected, max(0, len(machines) - 1))
        else:
            self.selected = min(self.selected, max(0, len(machines) - 1))
        self.last_refresh = time.monotonic()

    def maintain(self) -> None:
        """Harvest work and perform the low-frequency status refresh."""
        changed = False
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            self.pending = ""
            self.message = result.message
            if not result.ok:
                self.error = result.message
            changed = True
        now = time.monotonic()
        due = (
            self.refresh_seconds > 0
            and now - self.last_refresh >= self.refresh_seconds
        )
        if changed or (due and not self.pending):
            self.refresh()

    def move(self, amount: int) -> None:
        if self.modal == "actions":
            actions = self.actions
            if actions:
                self.action_index = max(
                    0, min(len(actions) - 1, self.action_index + amount))
            return
        if self.machines:
            self.selected = max(
                0, min(len(self.machines) - 1, self.selected + amount))
            self.action_index = 0

    def open_actions(self) -> None:
        if self.selected_vm and self.actions and not self.pending:
            self.modal = "actions"
            self.action_index = min(
                self.action_index, max(0, len(self.actions) - 1))

    def primary(self) -> None:
        """Enter: launch a stopped VM, focus an active one's existing tab."""
        vm = self.selected_vm
        if vm is None or self.pending:
            return
        if vm.startable:
            self._begin(Action(
                "launch", "Launch in a Kilix tab",
                "Stream VirtualBoxVM through kilix run."))
        elif vm.active:
            self._begin(Action(
                "focus", "Focus its Kilix tab",
                "Switch to the tab whose command owns this VM UUID."))
        else:
            self.message = f"{vm.name} cannot be selected while {vm.label_state}."

    def choose_action(self) -> None:
        actions = self.actions
        if not actions or not (0 <= self.action_index < len(actions)):
            return
        action = actions[self.action_index]
        if action.confirm:
            self.confirm_action = action
            self.modal = "confirm"
        else:
            self.modal = ""
            self._begin(action)

    def confirm(self, yes: bool) -> None:
        action = self.confirm_action
        self.confirm_action = None
        self.modal = ""
        if yes and action:
            self._begin(action)
        elif action:
            self.message = f"Cancelled: {action.label}."

    def _begin(self, action: Action) -> None:
        vm = self.selected_vm
        if vm is None or self.pending:
            return
        self.pending = f"{action.label}: {vm.name}"
        self.message = ""
        self.error = ""

        def work() -> None:
            try:
                result = self.client.execute(
                    vm,
                    action.ident,
                    run_options=self.run_options,
                    fullscreen=self.fullscreen,
                )
            except Exception as error:
                result = OperationResult(
                    False,
                    f"{type(error).__name__}: {error}",
                    self.client.preview(
                        vm, action.ident, run_options=self.run_options,
                        fullscreen=self.fullscreen),
                )
            self._results.put(result)

        threading.Thread(
            target=work,
            name=f"kilix-vbox-{action.ident}",
            daemon=True,
        ).start()

    def preview(self, action: Action) -> str:
        vm = self.selected_vm
        if vm is None:
            return ""
        return shlex.join(self.client.preview(
            vm,
            action.ident,
            run_options=self.run_options,
            fullscreen=self.fullscreen,
        ))


def _put(surface, row: int, col: int, text: str, attr: int = 0) -> None:
    """Write like the canonical kilix-tui text renderer: clipped, never fatal."""
    try:
        height, width = surface.getmaxyx()
    except Exception:
        height, width = 24, 80
    if not (0 <= row < height) or col >= width:
        return
    if col < 0:
        text = text[-col:]
        col = 0
    text = text[: max(0, width - col - (1 if row == height - 1 else 0))]
    if not text:
        return
    try:
        surface.addstr(row, col, text, attr)
    except Exception:
        pass


def _short_path(value: str) -> str:
    if not value:
        return "—"
    home = str(Path.home())
    return "~" + value[len(home):] if value.startswith(home + "/") else value


def _fit(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    return value[: max(0, width - 1)] + "…"


def _state_attr(vm: VirtualMachine) -> int:
    if vm.running:
        return tango.attr("accent")
    if vm.paused:
        return tango.attr("title")
    if vm.error:
        return tango.attr("alert")
    return tango.attr("muted")


def _tunnel_attr(vm: VirtualMachine) -> int:
    if vm.active_tunnels:
        return tango.attr("accent")
    if vm.active and vm.tunnels:
        return tango.attr("alert")
    return tango.attr("muted")


def _summary(state: State) -> str:
    live = sum(vm.active for vm in state.machines)
    tunnels = sum(bool(vm.active_tunnels) for vm in state.machines)
    if state.pending:
        return f"WORKING · {state.pending}"
    return (
        f"{len(state.machines)} VMS · {live} ACTIVE · "
        f"{tunnels} TUNNEL{'S' if tunnels != 1 else ''} UP"
    )


def _footer(state: State) -> str:
    if state.modal == "confirm":
        return "y confirm · n/Esc cancel"
    if state.modal == "actions":
        return "↑/↓ choose · Enter run · Esc close"
    return "↑↓ · Enter launch/focus · q quit · a actions · Tab view · r refresh"


def _draw_message(
    surface, state: State, top: int, left: int, width: int,
) -> int:
    message = state.error or state.message
    if not message:
        return top
    attr = tango.attr("alert" if state.error else "accent")
    _put(surface, top, left, message.replace("\n", " · ")[:width], attr)
    return top + 1


def _draw_machine_list(
    surface,
    state: State,
    top: int,
    left: int,
    height: int,
    width: int,
) -> None:
    state.row_hits = []
    if height <= 0 or width <= 0:
        return
    _put(surface, top, left, "VPN MACHINES", tango.attr("title"))
    if not state.machines:
        message = state.error or "No registered VirtualBox machines."
        for offset, line in enumerate(textwrap.wrap(message, max(1, width))):
            if offset + 2 >= height:
                break
            _put(surface, top + 2 + offset, left, line)
        return
    row = top + 2
    for index, vm in enumerate(state.machines):
        if row >= top + height:
            break
        marker = "▶" if index == state.selected else " "
        state.row_hits.append((row, index))
        if width >= 40:
            state_width = 11
            tunnel_width = 13
            name_width = max(10, width - state_width - tunnel_width - 4)
            name = f"{marker} {_fit(vm.name, name_width):<{name_width}}"
            machine_state = vm.label_state[:state_width].upper()
            tunnel = vm.tunnel_status[:tunnel_width].upper()
            if index == state.selected:
                line = (
                    f"{name} {machine_state:<{state_width}} "
                    f"{tunnel:<{tunnel_width}}"
                )
                _put(
                    surface, row, left, line.ljust(width)[:width],
                    tango.attr("selected"),
                )
            else:
                _put(surface, row, left, name)
                _put(
                    surface, row, left + name_width + 3,
                    machine_state, _state_attr(vm),
                )
                _put(
                    surface, row, left + name_width + state_width + 4,
                    tunnel, _tunnel_attr(vm),
                )
        else:
            suffix = f" · {vm.label_state} · {vm.tunnel_status}"
            _put(
                surface, row, left,
                f"{marker} {vm.name}{suffix}"[:width],
                tango.attr("selected") if index == state.selected else 0,
            )
        row += 1


def _overview_lines(vm: VirtualMachine) -> list[tuple[str, str]]:
    resources = f"{vm.cpus or '?'} vCPU · {vm.memory_mb or '?'} MiB RAM"
    if vm.vram_mb:
        resources += f" · {vm.vram_mb} MiB video"
    changed = vm.label_state
    if age := vm.age(datetime.now(timezone.utc)):
        changed += f" for {age}"
    return [
        ("State", changed),
        ("Guest", vm.os_type or "unknown"),
        ("Resources", resources),
        ("Session", vm.session_name or "none"),
        ("Guest Additions", vm.guest_additions or "not reporting"),
        ("UUID", vm.uuid),
        ("Config", _short_path(vm.config_file)),
    ]


def _network_lines(vm: VirtualMachine) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    if vm.guest_interfaces:
        for item in vm.guest_interfaces:
            kind = "Tunnel" if item.is_tunnel else "Guest"
            detail = f"{item.name or f'net{item.index}'} · {item.status or 'unknown'}"
            if item.ipv4:
                detail += f" · {item.ipv4}"
            lines.append((kind, detail))
    else:
        lines.append(("Guest", "network details unavailable"))
    for item in vm.host_adapters:
        detail = item.label
        if item.mac:
            detail += f" · {item.mac}"
        lines.append((f"Adapter {item.index}", detail))
    for item in vm.port_forwards:
        lines.append((item.name or "Forward", item.label))
    return lines


def _snapshot_lines(vm: VirtualMachine) -> list[tuple[str, str]]:
    if not vm.snapshots:
        return [("Snapshots", "none")]
    lines = [("Current", vm.current_snapshot or "none")]
    lines.extend((f"{index:02d}", name)
                 for index, name in enumerate(vm.snapshots, 1))
    return lines


def _help_lines(_vm: VirtualMachine) -> list[tuple[str, str]]:
    return [
        ("Enter", "launch a stopped VM, or focus an active VM's Kilix tab"),
        ("a", "open pause/resume, shutdown, save, reset, and power controls"),
        ("Tab / 1-4", "cycle the detail pages"),
        ("r", "refresh immediately; status also refreshes automatically"),
        ("↑ / ↓", "select a registered VM"),
        ("q", "leave the manager; running VMs and tabs are untouched"),
    ]


def _draw_details(
    surface,
    state: State,
    top: int,
    left: int,
    height: int,
    width: int,
) -> None:
    vm = state.selected_vm
    if vm is None or height <= 0 or width <= 0:
        return
    title = f"{DETAIL_TABS[state.detail_tab].upper()} · {vm.name}"
    _put(surface, top, left, title[:width], tango.attr("title"))
    if vm.error:
        lines = [("Error", vm.error)]
    elif state.detail_tab == 0:
        lines = _overview_lines(vm)
    elif state.detail_tab == 1:
        lines = _network_lines(vm)
    elif state.detail_tab == 2:
        lines = _snapshot_lines(vm)
    else:
        lines = _help_lines(vm)
    row = top + 2
    label_width = min(18, max(10, width // 3))
    for label, value in lines:
        if row >= top + height:
            break
        prefix = f"{label[:label_width]:<{label_width}}"
        _put(surface, row, left, prefix[:width], tango.attr("muted"))
        available = max(0, width - label_width - 1)
        wrapped = textwrap.wrap(
            value or "—", max(1, available),
            break_long_words=True, break_on_hyphens=False,
        ) or ["—"]
        for offset, line in enumerate(wrapped):
            if row >= top + height:
                break
            column = left + label_width + 1
            _put(surface, row, column, line[:available])
            row += 1


def _draw_modal(
    surface,
    state: State,
    top: int,
    left: int,
    height: int,
    width: int,
) -> None:
    vm = state.selected_vm
    if vm is None or height <= 0 or width <= 0:
        return
    if state.modal == "actions":
        _put(
            surface, top, left, f"ACTIONS · {vm.name}"[:width],
            tango.attr("title"),
        )
        row = top + 2
        for index, action in enumerate(state.actions):
            if row >= top + height:
                break
            marker = "▶" if index == state.action_index else " "
            warning = " !" if action.danger else ""
            text = f" {marker} {action.label}{warning}"
            if index == state.action_index:
                attr = tango.attr("danger" if action.danger else "selected")
                _put(surface, row, left, text.ljust(width)[:width], attr)
            else:
                _put(
                    surface, row, left, text[:width],
                    tango.attr("alert") if action.danger else 0,
                )
            row += 1
            if width >= 45 and row < top + height:
                _put(
                    surface, row, left + 4,
                    action.description[: max(0, width - 4)],
                    tango.attr("muted"),
                )
                row += 1
        return

    action = state.confirm_action
    if action is None:
        return
    row = top
    _put(
        surface, row, left, f"Confirm: {action.label} — {vm.name}"[:width],
        tango.attr("danger"),
    )
    row += 2
    for line in textwrap.wrap(action.description, max(1, width)):
        if row >= top + height - 4:
            break
        _put(surface, row, left, line)
        row += 1
    row += 1
    command = "$ " + state.preview(action)
    for line in textwrap.wrap(
        command, max(1, width),
        break_long_words=True, break_on_hyphens=False,
    ):
        if row >= top + height - 2:
            break
        _put(surface, row, left, line, tango.attr("muted"))
        row += 1
    if row < top + height - 1:
        _put(
            surface, row + 1, left,
            "y to proceed · n or Esc to cancel",
            tango.attr("muted"),
        )


def render(surface, state: State) -> None:
    state.maintain()
    try:
        surface_height, surface_width = surface.getmaxyx()
    except Exception:
        surface_height, surface_width = 24, 80
    if surface_height <= 0 or surface_width <= 0:
        return

    # The manager is the reference application for the shared Kilix shell.
    body = shell.draw(
        surface,
        title="VirtualBox VPN",
        sections=DETAIL_TABS,
        active=state.detail_tab,
        summary=_summary(state),
        footer=_footer(state),
        summary_role="accent" if state.pending else "muted",
    )

    top = body.top
    left = body.left
    height = body.height
    width = body.width
    top = _draw_message(surface, state, top, left, width)
    height = max(0, surface_height - top - 1)
    if state.modal:
        _draw_modal(surface, state, top, left, height, width)
    else:
        if width >= 88 and height >= 9:
            list_width = min(48, max(34, width * 43 // 100))
            _draw_machine_list(surface, state, top, left, height, list_width)
            separator = left + list_width
            for row in range(top, top + height):
                _put(
                    surface, row, separator, "│", tango.attr("muted"))
            _draw_details(
                surface, state, top, separator + 2, height,
                max(0, width - list_width - 2),
            )
        else:
            list_height = min(
                max(4, len(state.machines) + 2),
                max(4, height // 3),
            )
            _draw_machine_list(
                surface, state, top, left, min(height, list_height), width)
            detail_top = top + list_height
            if detail_top < top + height:
                _put(
                    surface, detail_top, left, "─" * max(0, width),
                    tango.attr("muted"),
                )
                detail_top += 1
            _draw_details(
                surface, state, detail_top, left,
                max(0, top + height - detail_top), width,
            )


def _mouse(key: int, state: State) -> bool:
    if key != curses.KEY_MOUSE or state.modal:
        return False
    try:
        _ident, _x, y, _z, button = curses.getmouse()
    except Exception:
        return True
    for row, index in state.row_hits:
        if row == y:
            already = state.selected == index
            state.selected = index
            if already and button & (
                getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
                | getattr(curses, "BUTTON1_CLICKED", 0)
            ):
                state.primary()
            return True
    return True


def handle(key: int, state: State) -> bool:
    if _mouse(key, state):
        return True
    if state.modal == "confirm":
        if key in (ord("y"), ord("Y")):
            state.confirm(True)
        elif key in (ord("n"), ord("N"), 27, ord("q"), ord("Q")):
            state.confirm(False)
        return True
    if state.modal == "actions":
        if key in (27, ord("q"), ord("Q"), ord("a")):
            state.modal = ""
        elif step := keymap.direction(key):
            state.move(step)
        elif key in keymap.SELECT:
            state.choose_action()
        return True
    if keymap.is_quit(key):
        return False
    if step := keymap.direction(key):
        state.move(step)
    elif key in (ord("\n"), ord("\r"), 343):
        state.primary()
    elif key == ord("a"):
        state.open_actions()
    elif key == 9:
        state.detail_tab = (state.detail_tab + 1) % len(DETAIL_TABS)
    elif ord("1") <= key <= ord("4"):
        state.detail_tab = key - ord("1")
    elif keymap.is_help(key):
        state.detail_tab = DETAIL_TABS.index("Help")
    elif keymap.is_refresh(key):
        state.refresh()
        state.message = "VirtualBox status refreshed."
    return True
