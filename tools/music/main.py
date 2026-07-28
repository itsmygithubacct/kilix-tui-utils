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

from kilix_tui import app, keys as keymap, proc  # noqa: E402

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
    height, width = surface.getmaxyx()
    surface.addstr(0, 0, "Kilix Music"[: width - 1])
    if not state.backend.available():
        surface.addstr(2, 0, "kilix-amp backend unavailable."[: width - 1])
        surface.addstr(3, 0,
                       "This front end drives kilix-amp over a control socket;"[: width - 1])
        surface.addstr(4, 0,
                       "kilix-amp does not ship the headless backend yet."[: width - 1])
        surface.addstr(6, 0, f"expected socket: {state.backend.path}"[: width - 1])
        surface.addstr(7, 0,
                       "Use the Kilix 95 Media Player until then."[: width - 1])
        surface.addstr(height - 1, 0, "r retry · q quit"[: width - 1])
        return
    if state.backend.error:
        surface.addstr(2, 0, state.backend.error[: width - 1])
    playing = state.status.get("state", "stopped")
    title = state.status.get("title", "")
    position = float(state.status.get("pos", 0) or 0)
    length = float(state.status.get("len", 0) or 0)
    surface.addstr(2, 0, f"{playing}  {title}"[: width - 1])
    if length:
        surface.addstr(3, 0,
                       f"{proc.human_duration(position)} / "
                       f"{proc.human_duration(length)}  "
                       f"{proc.bar(position / length, max(0, width - 24))}"[: width - 1])
    for index, item in enumerate(state.playlist):
        row = 5 + index
        if row >= height - 1:
            break
        marker = ">" if index == state.selected else " "
        surface.addstr(row, 0, f"{marker} {os.path.basename(item)}"[: width - 1])
    surface.addstr(height - 1, 0,
                   "space play/pause · n next · p prev · r refresh · q quit"[: width - 1])


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
