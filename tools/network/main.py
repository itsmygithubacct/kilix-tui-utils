"""kilix-network — links and saved connections, in the canonical shell.

Settles the network boundary the rollout record left open. The everyday
surface lives here: what every link is doing, and the saved NetworkManager
connections brought up on Enter or taken down after a confirmation. Creating
connections and entering secrets deliberately stay in `nmtui` — a password
prompt belongs to NetworkManager's own agent, and a second implementation of
it here would be a second thing to get wrong.

Taking a link down confirms first because the link being cut is often the one
carrying the keystroke: over SSH, disconnect severs the hand that pressed it.
Bringing one up does not — that is what you came to do, and it cuts nothing.

Without NetworkManager the tool degrades to a read-only view of
`/sys/class/net` rather than an error, so a bare machine still answers
"what is the network doing" in the same place.
"""
from __future__ import annotations

import subprocess

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402

SECTIONS = ("Devices", "Connections")


def control() -> str | None:
    from shutil import which
    return "nmcli" if which("nmcli") else None


def _nmcli(args: list[str]) -> str:
    """Stdout of one read-only terse query, or "" when it cannot answer."""
    try:
        return subprocess.run(["nmcli", "-t", *args], capture_output=True,
                              text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _act(argv: list[str]) -> str:
    """Run one state-changing action and return what it said."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=60, check=False)
        return (result.stdout or result.stderr).strip() or "done"
    except FileNotFoundError:
        return f"({argv[0]} not installed)"
    except (OSError, subprocess.SubprocessError) as error:
        return f"({error})"


def split_terse(line: str) -> list[str]:
    """Split one `nmcli -t` line on `:`, honouring its backslash escapes.

    Terse mode escapes `:` and `\\` inside values — an SSID can contain
    either — so a plain str.split would shear those values apart.
    """
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def devices() -> list[dict[str, str]]:
    """Every link NetworkManager manages, loopback excluded."""
    rows: list[dict[str, str]] = []
    listing = _nmcli(["-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
    for line in listing.splitlines():
        fields = split_terse(line)
        if len(fields) < 4 or fields[1] == "loopback":
            continue
        rows.append({"device": fields[0], "type": fields[1],
                     "state": fields[2], "connection": fields[3]})
    return rows


def connections() -> list[dict[str, object]]:
    """Every saved connection; active means a device currently carries it."""
    rows: list[dict[str, object]] = []
    listing = _nmcli(["-f", "NAME,UUID,TYPE,DEVICE", "connection", "show"])
    for line in listing.splitlines():
        fields = split_terse(line)
        if len(fields) < 4:
            continue
        rows.append({"name": fields[0], "uuid": fields[1], "type": fields[2],
                     "device": fields[3], "active": bool(fields[3])})
    return rows


def fallback_devices() -> list[dict[str, str]]:
    """The read-only `/sys` view for a machine without NetworkManager."""
    return [{"device": name, "type": "", "state": state, "connection": ""}
            for name, state in proc.network_links()]


def up_action(section: int, row: dict) -> tuple[str, list[str]]:
    if section == 0:
        return (f"connect {row['device']}",
                ["nmcli", "device", "connect", str(row["device"])])
    # By uuid, not name: two saved connections can share a display name.
    return (f"bring up {row['name']}",
            ["nmcli", "connection", "up", "uuid", str(row["uuid"])])


def down_action(section: int, row: dict) -> tuple[str, list[str]]:
    if section == 0:
        return (f"disconnect {row['device']}",
                ["nmcli", "device", "disconnect", str(row["device"])])
    return (f"take down {row['name']}",
            ["nmcli", "connection", "down", "uuid", str(row["uuid"])])


def _is_up(state: str) -> bool:
    return state.startswith("connected") or state == "up"


class State:
    def __init__(self) -> None:
        self.control = control()
        self.section = 0
        self.selected = 0
        self.devices: list[dict[str, str]] = []
        self.connections: list[dict[str, object]] = []
        self.message = ""
        self.confirm: tuple[str, list[str]] | None = None
        self.refresh()

    def refresh(self) -> None:
        if self.control:
            self.devices = devices()
            self.connections = connections()
        else:
            self.devices = fallback_devices()
            self.connections = []
        self.selected = min(self.selected, max(0, len(self.rows()) - 1))

    def rows(self) -> list[dict]:
        return self.devices if self.section == 0 else self.connections

    @property
    def active(self) -> dict | None:
        rows = self.rows()
        return rows[self.selected] if self.selected < len(rows) else None


def render(surface, state: State) -> None:
    rows = state.rows()
    if state.confirm is not None:
        summary = f"Confirm: {state.confirm[0]}"
    elif state.message:
        summary = state.message.splitlines()[0]
    elif state.control is None:
        summary = "NetworkManager (nmcli) not found — read-only view"
    elif state.section == 0:
        up = sum(1 for row in rows if _is_up(str(row["state"])))
        summary = f"{len(rows)} links · {up} up"
    else:
        active = sum(1 for row in rows if row["active"])
        summary = f"{len(rows)} saved connections · {active} active"
    body = shell.draw(
        surface,
        title="Network",
        sections=SECTIONS,
        active=state.section,
        summary=summary,
        footer=(
            "y proceed · n/Esc cancel"
            if state.confirm is not None
            else "1-2 section · Enter up · d down · r refresh · q quit"
            if state.control else "r refresh · q quit"
        ),
        summary_role=(
            "danger" if state.confirm is not None
            else "accent" if state.message
            else "alert" if state.control is None else "muted"
        ),
    )
    row = body.top
    if state.confirm is not None:
        shell.put(surface, row, body.left,
                  f"$ {' '.join(state.confirm[1])}",
                  shell.tango.attr("muted"))
        shell.put(surface, row + 2, body.left,
                  "y to proceed · any other key to cancel",
                  shell.tango.attr("danger"))
        return
    if state.section == 0:
        for index, link in enumerate(rows):
            if row >= body.bottom:
                break
            selected = index == state.selected
            marker = "▶" if selected else " "
            up = _is_up(str(link["state"]))
            shell.put(
                surface, row, body.left,
                f"{marker} {str(link['device']):<12.12} "
                f"{str(link['type']):<10.10} {str(link['state']):<16.16} "
                f"{link['connection']}",
                shell.tango.attr("selected") if selected
                else (0 if up else shell.tango.attr("muted")),
            )
            row += 1
    else:
        for index, connection in enumerate(rows):
            if row >= body.bottom:
                break
            selected = index == state.selected
            marker = "▶" if selected else " "
            star = "*" if connection["active"] else " "
            shell.put(
                surface, row, body.left,
                f"{marker}{star} {str(connection['name']):<24.24} "
                f"{str(connection['type']):<12.12} "
                f"{str(connection['device']) or '—'}",
                shell.tango.attr("selected") if selected
                else (0 if connection["active"]
                      else shell.tango.attr("muted")),
            )
            row += 1
    if state.message:
        row = min(row + 1, body.bottom - 1)
        for line in state.message.splitlines()[:max(0, body.bottom - row)]:
            shell.put(surface, row, body.left, line,
                      shell.tango.attr("muted"))
            row += 1


def handle(key: int, state: State) -> bool:
    if state.confirm is not None:
        label, argv = state.confirm
        state.confirm = None
        if key in (ord("y"), ord("Y")):
            state.message = f"$ {' '.join(argv)}\n" + _act(argv)
            state.refresh()
        else:
            state.message = f"cancelled: {label}"
        return True
    if keymap.is_quit(key):
        return False
    if key == ord("\t"):
        state.section = (state.section + 1) % len(SECTIONS)
        state.selected = 0
        state.message = ""
    elif key in (ord("1"), ord("2")):
        state.section = key - ord("1")
        state.selected = 0
        state.message = ""
    elif step := keymap.direction(key):
        rows = state.rows()
        if rows:
            state.selected = max(0, min(len(rows) - 1,
                                        state.selected + step))
    elif key in keymap.SELECT and state.control:
        if (row := state.active) is not None:
            _label, argv = up_action(state.section, row)
            state.message = f"$ {' '.join(argv)}\n" + _act(argv)
            state.refresh()
    elif key == ord("d") and state.control:
        if (row := state.active) is not None:
            state.confirm = down_action(state.section, row)
    elif keymap.is_refresh(key):
        state.message = ""
        state.refresh()
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle_:
            handle_.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
