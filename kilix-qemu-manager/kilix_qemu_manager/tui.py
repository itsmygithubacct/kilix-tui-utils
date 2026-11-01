"""Responsive curses interface for the Kilix QEMU manager."""
from __future__ import annotations

import curses
import queue
import shlex
import textwrap
import threading
import time
from dataclasses import dataclass
from typing import Any

from kilix_desk import tango
from kilix_tui import keys as keymap, shell

from . import store
from .backend import DESTRUCTIVE, OperationResult, QemuManager
from .model import VmStatus

TITLE = "QEMU VMs"
DETAIL_TABS = ("Overview", "Disks", "Help")


@dataclass(frozen=True)
class Action:
    ident: str
    label: str
    description: str
    confirm: bool = False
    danger: bool = False


LAUNCH = Action("launch", "Launch in a Kilix tab",
                "Run the VM through kilix run in a new tab titled QEMU: <name>.")
FOCUS = Action("focus", "Focus its Kilix tab",
               "Switch to the tab whose command owns this VM's QMP socket.")


def actions_for(status: VmStatus) -> list[Action]:
    actions: list[Action] = []
    if status.startable:
        actions.append(LAUNCH)
    elif status.pane_id:
        actions.append(FOCUS)
    if status.qmp_ok and status.running:
        actions.append(Action("pause", "Pause", "Stop guest CPUs (QMP stop)."))
    elif status.qmp_ok and status.paused:
        actions.append(Action("resume", "Resume",
                              "Continue guest CPUs (QMP cont)."))
    if status.qmp_ok:
        actions.extend([
            Action("shutdown", "ACPI shutdown",
                   "Press the guest's ACPI power button (system_powerdown).",
                   confirm=True),
            Action("reset", "Reset",
                   "Hard-reset the VM without a guest shutdown "
                   "(system_reset).", confirm=True, danger=True),
            Action("poweroff", "Power off",
                   "End QEMU immediately (quit); unsaved guest data is lost.",
                   confirm=True, danger=True),
        ])
    if status.live and status.vm.has_ssh:
        actions.append(Action(
            "ssh", "Open SSH",
            f"Open a Kilix tab running ssh -p {status.vm.ssh_port} "
            f"{status.vm.ssh_user}@127.0.0.1."))
    return actions


class State:
    """All mutable state, including one serialized background operation."""

    def __init__(
        self,
        manager: QemuManager | None = None,
        *,
        size: str | None = None,
        refresh_seconds: float = 3.0,
    ) -> None:
        self.manager = manager or QemuManager()
        self.size = size
        self.refresh_seconds = max(0.0, refresh_seconds)
        self.machines: list[VmStatus] = []
        self.problems: list[str] = []
        self.tree_error = ""
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
        self.disk_cache: dict[str, list[dict[str, Any]]] = {}
        self._results: queue.SimpleQueue[OperationResult] = queue.SimpleQueue()
        self.refresh()

    @property
    def selected_vm(self) -> VmStatus | None:
        if not self.machines:
            return None
        self.selected = max(0, min(self.selected, len(self.machines) - 1))
        return self.machines[self.selected]

    @property
    def actions(self) -> list[Action]:
        status = self.selected_vm
        return actions_for(status) if status else []

    def refresh(self) -> None:
        current = self.selected_vm.vm.id if self.selected_vm else ""
        try:
            machines, problems, tree_error = self.manager.inventory()
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            self.last_refresh = time.monotonic()
            return
        self.machines, self.problems, self.tree_error = (
            machines, problems, tree_error)
        self.select_id(current)
        self.last_refresh = time.monotonic()

    def select_id(self, vm_id: str) -> None:
        for index, status in enumerate(self.machines):
            if status.vm.id == vm_id:
                self.selected = index
                return
        self.selected = min(self.selected, max(0, len(self.machines) - 1))

    def maintain(self) -> None:
        changed = False
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            self.pending = ""
            self.message = result.message
            self.error = "" if result.ok else result.message
            changed = True
        due = (self.refresh_seconds > 0
               and time.monotonic() - self.last_refresh >= self.refresh_seconds)
        if changed or (due and not self.pending):
            self.refresh()

    def move(self, amount: int) -> None:
        if self.modal == "actions":
            if self.actions:
                self.action_index = max(
                    0, min(len(self.actions) - 1, self.action_index + amount))
            return
        if self.machines:
            self.selected = max(
                0, min(len(self.machines) - 1, self.selected + amount))
            self.action_index = 0

    def toggle_favorite(self) -> None:
        status = self.selected_vm
        if status is None:
            return
        now = store.toggle_favorite(status.vm.id)
        self.message = (f"{status.vm.name} is a favorite." if now
                        else f"{status.vm.name} is no longer a favorite.")
        self.error = ""
        self.refresh()
        self.select_id(status.vm.id)

    def open_actions(self) -> None:
        if self.selected_vm and self.actions and not self.pending:
            self.modal = "actions"
            self.action_index = min(self.action_index,
                                    max(0, len(self.actions) - 1))

    def primary(self) -> None:
        """Enter: launch a stopped VM, focus a live one's existing tab."""
        status = self.selected_vm
        if status is None or self.pending:
            return
        if status.startable:
            self._begin(LAUNCH)
        elif status.pane_id:
            self._begin(FOCUS)
        elif status.state == "starting":
            self.message = f"{status.vm.name} is starting."
        elif not status.tabs_known:
            self.error = self.message = (
                f"{status.vm.name} is live, and Kilix could not be asked for "
                f"its tab ({self.tree_error or 'not inside Kilix'}).")
        else:
            self.error = self.message = (
                f"{status.vm.name} is live, no Kilix tab — not opening a "
                "second copy.")

    def choose_action(self) -> None:
        actions = self.actions
        if not actions or not 0 <= self.action_index < len(actions):
            return
        action = actions[self.action_index]
        if action.confirm or action.ident in DESTRUCTIVE:
            self.confirm_action = action
            self.modal = "confirm"
        else:
            self.modal = ""
            self._begin(action)

    def confirm(self, yes: bool) -> None:
        action, self.confirm_action, self.modal = self.confirm_action, None, ""
        if yes and action:
            self._begin(action)
        elif action:
            self.message = f"Cancelled: {action.label}."

    def _begin(self, action: Action) -> None:
        status = self.selected_vm
        if status is None or self.pending:
            return
        vm = status.vm
        self.pending = f"{action.label}: {vm.name}"
        self.message = self.error = ""

        def work() -> None:
            try:
                result = self.manager.execute(vm, action.ident, size=self.size)
            except Exception as error:
                result = OperationResult(False, f"{type(error).__name__}: {error}")
            self._results.put(result)

        threading.Thread(target=work, name=f"kilix-qemu-{action.ident}",
                         daemon=True).start()

    def preview(self, action: Action) -> str:
        status = self.selected_vm
        if status is None:
            return ""
        command = self.manager.preview(status.vm, action.ident, self.size,
                                       pane_id=status.pane_id)
        if command and command[0] == "qmp":
            return f"QMP unix:{command[1]} ← {command[2]}"
        return shlex.join(command)

    def disks(self) -> list[dict[str, Any]]:
        status = self.selected_vm
        if status is None:
            return []
        if status.vm.id not in self.disk_cache:
            try:
                self.disk_cache[status.vm.id] = self.manager.disk_info(status.vm)
            except Exception as error:
                self.disk_cache[status.vm.id] = [{"path": "", "error": str(error)}]
        return self.disk_cache[status.vm.id]


# ── drawing ──────────────────────────────────────────────────────────────────


def _put(surface, row: int, col: int, text: str, attr: int = 0) -> None:
    try:
        height, width = surface.getmaxyx()
    except Exception:
        height, width = 24, 80
    if not (0 <= row < height) or col >= width:
        return
    if col < 0:
        text, col = text[-col:], 0
    text = text[: max(0, width - col - (1 if row == height - 1 else 0))]
    if text:
        try:
            surface.addstr(row, col, text, attr)
        except Exception:
            pass


def _fit(value: str, width: int) -> str:
    if width <= 0:
        return ""
    return value if len(value) <= width else value[: max(0, width - 1)] + "…"


def _state_attr(status: VmStatus) -> int:
    if status.outside_kilix:
        return tango.attr("alert")
    if status.running:
        return tango.attr("accent")
    if status.live:
        return tango.attr("title")
    return tango.attr("muted")


def _summary(state: State) -> str:
    if state.pending:
        return f"WORKING · {state.pending}"
    live = sum(status.live for status in state.machines)
    favorites = sum(status.favorite for status in state.machines)
    return (f"{len(state.machines)} VMS · {live} LIVE · "
            f"{favorites} FAVORITE{'S' if favorites != 1 else ''}")


def _footer(state: State) -> str:
    if state.modal == "confirm":
        return "y confirm · n/Esc cancel"
    if state.modal == "actions":
        return "↑/↓ choose · Enter run · Esc close"
    return ("↑↓ · Enter launch/focus · f favorite · a actions · Tab view · "
            "r refresh · q quit")


def _draw_list(surface, state: State, top: int, left: int, height: int,
               width: int) -> None:
    state.row_hits = []
    if height <= 0 or width <= 0:
        return
    _put(surface, top, left, "VIRTUAL MACHINES", tango.attr("title"))
    if not state.machines:
        message = state.error or (
            "No QEMU VMs in the catalog. Add one with "
            "kilix-qemu-manager import NAME -- qemu-system-x86_64 …, or "
            "kilix-qemu-manager discover.")
        for offset, line in enumerate(textwrap.wrap(message, max(1, width))):
            if offset + 2 >= height:
                break
            _put(surface, top + 2 + offset, left, line)
        return
    row = top + 2
    for index, status in enumerate(state.machines):
        if row >= top + height:
            break
        state.row_hits.append((row, index))
        marker = "▶" if index == state.selected else " "
        star = "★" if status.favorite else " "
        label = status.label.upper()
        if width >= 30:
            state_width = min(20, width - 14)
            name_width = max(8, width - state_width - 5)
            name = f"{marker}{star} {_fit(status.vm.name, name_width):<{name_width}}"
            if index == state.selected:
                line = f"{name} {label[:state_width]}"
                _put(surface, row, left, line.ljust(width)[:width],
                     tango.attr("selected"))
            else:
                _put(surface, row, left, name)
                _put(surface, row, left + len(name) + 1,
                     label[:state_width], _state_attr(status))
        else:
            _put(surface, row, left,
                 f"{marker}{star} {status.vm.name} · {label}"[:width],
                 tango.attr("selected") if index == state.selected else 0)
        row += 1


def _overview(state: State, status: VmStatus) -> list[tuple[str, str]]:
    vm = status.vm
    lines = [
        ("State", status.label + (f" ({status.detail})" if status.detail else "")),
        ("Kilix tab", f"pane {status.pane_id}" if status.pane_id
         else ("none" if status.tabs_known else
               f"unknown — {state.tree_error or 'not inside Kilix'}")),
        ("Favorite", "yes" if status.favorite else "no"),
        ("Last launch", status.last_launch or "never"),
        ("Id", vm.id),
        ("Binary", vm.qemu),
        ("Arguments", f"{len(vm.argv)} (kilix-qemu-manager list --json shows "
                      "them)"),
        ("QMP", vm.qmp),
        ("Pidfile", vm.pidfile),
        ("SSH", f"ssh -p {vm.ssh_port} {vm.ssh_user}@127.0.0.1"
         if vm.has_ssh else "no forward"),
    ]
    if vm.notes:
        lines.append(("Notes", vm.notes))
    return lines


def _size(value: Any) -> str:
    if not isinstance(value, int):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "?"


def _disks(state: State) -> list[tuple[str, str]]:
    rows = state.disks()
    if not rows:
        return [("Disks", "none (diskless, or only read-only media)")]
    lines: list[tuple[str, str]] = []
    for index, row in enumerate(rows, 1):
        lines.append((f"Disk {index}", row.get("path", "")))
        if row.get("error"):
            lines.append(("", row["error"]))
            continue
        lines.append(("", f"{row.get('format') or '?'} · "
                          f"{_size(row.get('virtual_size'))} virtual · "
                          f"{_size(row.get('actual_size'))} on disk"))
        if row.get("backing"):
            lines.append(("Backing", row["backing"]))
        snapshots = row.get("snapshots") or []
        lines.append(("Snapshots", ", ".join(item["name"] for item in snapshots)
                      if snapshots else "none"))
    lines.append(("", "read-only view (qemu-img info -U); snapshots are not "
                      "created here"))
    return lines


def _help() -> list[tuple[str, str]]:
    return [
        ("Enter", "launch a stopped VM, or focus a live VM's Kilix tab"),
        ("f", "mark or unmark a favorite; favorites sort first"),
        ("a", "pause/resume, ACPI shutdown, reset, power off, SSH"),
        ("Tab / 1-3", "overview, disks and snapshots, these keys"),
        ("r", "refresh now; status also refreshes on its own"),
        ("q", "leave the manager; running VMs and tabs are untouched"),
        ("CLI", "import, discover, favorite, remove: see --help"),
    ]


def _draw_details(surface, state: State, top: int, left: int, height: int,
                  width: int) -> None:
    status = state.selected_vm
    if status is None or height <= 0 or width <= 0:
        return
    _put(surface, top, left,
         f"{DETAIL_TABS[state.detail_tab].upper()} · {status.vm.name}"[:width],
         tango.attr("title"))
    if state.detail_tab == 0:
        lines = _overview(state, status)
    elif state.detail_tab == 1:
        lines = _disks(state)
    else:
        lines = _help()
    row = top + 2
    label_width = min(14, max(8, width // 3))
    available = max(1, width - label_width - 1)
    for label, value in lines:
        if row >= top + height:
            break
        _put(surface, row, left, f"{label[:label_width]:<{label_width}}",
             tango.attr("muted"))
        for line in textwrap.wrap(value or "—", available,
                                  break_long_words=True,
                                  break_on_hyphens=False) or ["—"]:
            if row >= top + height:
                break
            _put(surface, row, left + label_width + 1, line[:available])
            row += 1


def _draw_modal(surface, state: State, top: int, left: int, height: int,
                width: int) -> None:
    status = state.selected_vm
    if status is None or height <= 0 or width <= 0:
        return
    if state.modal == "actions":
        _put(surface, top, left, f"ACTIONS · {status.vm.name}"[:width],
             tango.attr("title"))
        row = top + 2
        for index, action in enumerate(state.actions):
            if row >= top + height:
                break
            chosen = index == state.action_index
            text = f" {'▶' if chosen else ' '} {action.label}" + (
                " !" if action.danger else "")
            if chosen:
                _put(surface, row, left, text.ljust(width)[:width],
                     tango.attr("danger" if action.danger else "selected"))
            else:
                _put(surface, row, left, text[:width],
                     tango.attr("alert") if action.danger else 0)
            row += 1
            if width >= 45 and row < top + height:
                _put(surface, row, left + 4, action.description[:width - 4],
                     tango.attr("muted"))
                row += 1
        return
    action = state.confirm_action
    if action is None:
        return
    _put(surface, top, left, f"Confirm: {action.label} — {status.vm.name}"[:width],
         tango.attr("danger"))
    row = top + 2
    for line in textwrap.wrap(action.description, max(1, width)):
        if row >= top + height - 4:
            break
        _put(surface, row, left, line)
        row += 1
    row += 1
    for line in textwrap.wrap("$ " + state.preview(action), max(1, width),
                              break_long_words=True, break_on_hyphens=False):
        if row >= top + height - 2:
            break
        _put(surface, row, left, line, tango.attr("muted"))
        row += 1
    if row < top + height - 1:
        _put(surface, row + 1, left, "y to proceed · n or Esc to cancel",
             tango.attr("muted"))


def render(surface, state: State) -> None:
    state.maintain()
    try:
        surface_height, _surface_width = surface.getmaxyx()
    except Exception:
        surface_height = 24
    if surface_height <= 0:
        return
    body = shell.draw(
        surface, title=TITLE, sections=DETAIL_TABS, active=state.detail_tab,
        summary=_summary(state), footer=_footer(state),
        summary_role="accent" if state.pending else "muted",
    )
    top, left, width = body.top, body.left, body.width
    message = state.error or state.message
    if message and width > 0:
        _put(surface, top, left, message.replace("\n", " · ")[:width],
             tango.attr("alert" if state.error else "accent"))
        top += 1
    # The shell reserves its own rows (footer, and a tip when it shows one).
    height = max(0, body.top + body.height - top)
    if state.modal:
        _draw_modal(surface, state, top, left, height, width)
    elif width >= 88 and height >= 9:
        list_width = min(44, max(34, width * 40 // 100))
        _draw_list(surface, state, top, left, height, list_width)
        for row in range(top, top + height):
            _put(surface, row, left + list_width, "│", tango.attr("muted"))
        _draw_details(surface, state, top, left + list_width + 2, height,
                      max(0, width - list_width - 2))
    else:
        list_height = min(max(4, len(state.machines) + 2), max(4, height // 3))
        _draw_list(surface, state, top, left, min(height, list_height), width)
        detail_top = top + list_height
        if detail_top < top + height:
            _put(surface, detail_top, left, "─" * max(0, width),
                 tango.attr("muted"))
            detail_top += 1
        _draw_details(surface, state, detail_top, left,
                      max(0, top + height - detail_top), width)


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
                    | getattr(curses, "BUTTON1_CLICKED", 0)):
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
    elif key == ord("f"):
        state.toggle_favorite()
    elif key == ord("a"):
        state.open_actions()
    elif key == 9:
        state.detail_tab = (state.detail_tab + 1) % len(DETAIL_TABS)
    elif ord("1") <= key <= ord("3"):
        state.detail_tab = key - ord("1")
    elif keymap.is_help(key):
        state.detail_tab = DETAIL_TABS.index("Help")
    elif keymap.is_refresh(key):
        state.disk_cache.clear()
        state.refresh()
        state.message = "QEMU status refreshed."
    return True
