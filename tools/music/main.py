"""kilix-music — a player driving kilix-amp over its control socket.

kilix-amp owns decoding and mixing; this is only a front end, so there is one
playback implementation rather than two that drift. The protocol is a versioned
contract between two repositories: this client declares the version it speaks
and reports a mismatch rather than guessing.

kilix-amp does not ship the headless backend yet. Until it does, this renders a
clear unavailable state and explains what is missing — it never fails at launch,
because it is installed unconditionally alongside the other tools.
"""
from __future__ import annotations

import json
import socket

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402

PROTOCOL_VERSION = 1


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(
        os.path.expanduser("~"), ".local/gpu_terminal/kilix/session")
    return os.environ.get("KILIX_AMP_SOCKET",
                          os.path.join(runtime, "kilix-amp.sock"))


class Backend:
    """Thin client for kilix-amp's control socket."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or socket_path()
        self.error = ""
        self.version: int | None = None

    def available(self) -> bool:
        return os.path.exists(self.path)

    def command(self, name: str, **fields: object) -> dict:
        if not self.available():
            self.error = "kilix-amp is not running with a control socket"
            return {}
        payload = json.dumps({"cmd": name, "protocol": PROTOCOL_VERSION,
                              **fields}) + "\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(self.path)
                client.sendall(payload.encode("utf-8"))
                data = client.recv(65536).decode("utf-8", errors="replace")
        except (OSError, socket.timeout) as failure:
            self.error = f"control socket: {failure}"
            return {}
        try:
            reply = json.loads(data.splitlines()[0]) if data.strip() else {}
        except (ValueError, IndexError):
            self.error = "malformed reply from kilix-amp"
            return {}
        if (their := reply.get("protocol")) and their != PROTOCOL_VERSION:
            self.version = int(their)
            self.error = (f"kilix-amp speaks protocol {their}, this client "
                          f"speaks {PROTOCOL_VERSION}")
            return {}
        self.error = ""
        return reply


class State:
    def __init__(self) -> None:
        self.backend = Backend()
        self.status: dict = {}
        self.playlist: list[str] = []
        self.selected = 0
        self.refresh()

    def refresh(self) -> None:
        self.status = self.backend.command("state") or {}
        listing = self.backend.command("playlist") or {}
        self.playlist = [str(i) for i in listing.get("items", [])]


def render(surface, state: State) -> None:
    available = state.backend.available()
    playing = str(state.status.get("state", "stopped"))
    title = str(state.status.get("title", ""))
    summary = (
        f"{playing} · {title}".rstrip(" ·")
        if available else "kilix-amp backend unavailable"
    )
    body = shell.draw(
        surface,
        title="Music",
        sections=("Playlist",),
        summary=summary,
        footer=(
            "space play/pause · n next · p prev · r refresh · q quit"
            if available else "r retry · q quit"
        ),
        summary_role="alert" if not available or state.backend.error else "muted",
    )
    if not state.backend.available():
        shell.put(surface, body.top, body.left,
                  "This front end drives kilix-amp over a control socket;")
        shell.put(surface, body.top + 1, body.left,
                  "kilix-amp does not ship the headless backend yet.")
        shell.put(surface, body.top + 3, body.left,
                  f"expected socket: {state.backend.path}",
                  shell.tango.attr("muted"))
        shell.put(surface, body.top + 4, body.left,
                  "Use the Kilix 95 Media Player until then.")
        return
    if state.backend.error:
        shell.put(surface, body.top, body.left, state.backend.error,
                  shell.tango.attr("alert"))
    position = float(state.status.get("pos", 0) or 0)
    length = float(state.status.get("len", 0) or 0)
    row = body.top
    if length:
        shell.put(
            surface, row, body.left,
            f"{proc.human_duration(position)} / "
            f"{proc.human_duration(length)}  "
            f"{proc.bar(position / length, max(0, body.width - 24))}",
            shell.tango.attr("accent"),
        )
        row += 2
    for index, item in enumerate(state.playlist):
        list_row = row + index
        if list_row >= body.bottom:
            break
        selected = index == state.selected
        marker = "▶" if selected else " "
        shell.put(
            surface, list_row, body.left,
            f"{marker} {os.path.basename(item)}",
            shell.tango.attr("selected") if selected else 0,
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, state) + "\n")
        return 0

    def handle(key: int, s: State) -> bool:
        if keymap.is_quit(key):
            return False
        if key == ord(" "):
            s.backend.command("toggle"); s.refresh()
        elif key == ord("n"):
            s.backend.command("next"); s.refresh()
        elif key == ord("p"):
            s.backend.command("previous"); s.refresh()
        elif (step := keymap.direction(key)) and s.playlist:
            s.selected = max(0, min(len(s.playlist) - 1, s.selected + step))
        elif key in keymap.SELECT and s.playlist:
            s.backend.command("play", index=s.selected); s.refresh()
        elif keymap.is_refresh(key):
            s.refresh()
        return True

    return app.run(render, state, handle=handle, tick_ms=1000)


if __name__ == "__main__":
    raise SystemExit(main())
