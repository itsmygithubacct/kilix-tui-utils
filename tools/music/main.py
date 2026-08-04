"""kilix-music — a player driving kilix-amp over its control socket.

kilix-amp owns decoding and mixing; this is only a front end, so there is one
playback implementation rather than two that drift. The protocol is a versioned
contract between two repositories: this client declares the version it speaks
and reports a mismatch rather than guessing.

kilix-amp ships the headless backend, so this starts one when none is running
and drives it. It still never fails at launch — it is installed unconditionally
alongside the other tools, and kilix-amp may not be built on a given machine —
so a missing backend is a state this renders and offers to fix, not an error.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_desk import sources  # noqa: E402
from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402

PROTOCOL_VERSION = 1
# Long enough for the backend to open its audio device on a slow disk, short
# enough that a wedged one does not look like a hung UI.
START_TIMEOUT = 5.0


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(
        os.path.expanduser("~"), ".local/gpu_terminal/kilix/session")
    return os.environ.get("KILIX_AMP_SOCKET",
                          os.path.join(runtime, "kilix-amp.sock"))


def clock(seconds: float) -> str:
    """M:SS, or H:MM:SS past an hour.

    `proc.human_duration` is minute-granular, which is right for an uptime and
    useless for a track: it renders every song shorter than a minute, and its
    whole first minute, as "0m".
    """
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _storage_home() -> str:
    """Kilix's writable root, resolved the way Kilix itself resolves it."""
    base = os.environ.get("GPU_TERMINAL_HOME") or os.path.expanduser(
        "~/.local/gpu_terminal")
    return os.environ.get("KILIX_STORAGE_HOME") or os.path.join(base, "kilix")


def backend_executable() -> str:
    """The kilix-amp binary, or "" when it is not built on this machine.

    Kilix 95 installs catalog apps under its own data directory, and a
    development checkout has it built in place; neither is on PATH.
    """
    override = os.environ.get("KILIX_AMP", "")
    if override:
        return override if os.access(override, os.X_OK) else ""
    found = shutil.which("kilix-amp")
    if found:
        return found
    data = os.environ.get("KILIX_DATA_HOME") or os.path.join(
        _storage_home(), "data")
    candidates = (
        os.path.join(data, "desktop-apps", "kilix-amp", "kilix-amp"),
        os.path.join(sources.component_dir("kilix-apps/kilix-amp"),
                     "kilix-amp"),
    )
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return candidate
    return ""


class Backend:
    """Thin client for kilix-amp's control socket."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or socket_path()
        self.error = ""
        self.version: int | None = None

    def available(self) -> bool:
        return os.path.exists(self.path)

    def installed(self) -> bool:
        return bool(backend_executable())

    def start(self, timeout: float = START_TIMEOUT) -> bool:
        """Launch a headless backend and wait for its socket to appear."""
        if self.available():
            return True
        executable = backend_executable()
        if not executable:
            self.error = "kilix-amp is not built on this machine"
            return False
        # The socket is passed explicitly rather than left to the default, so
        # the backend cannot resolve a different path from a different
        # environment than the one this client just computed.
        command = [executable, "--headless", "--socket", self.path]
        music = os.path.expanduser("~/Music")
        if os.path.isdir(music):
            command.append(music)
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Its own session: the backend outlives this front end, which
                # is the point of putting playback behind a socket.
                start_new_session=True,
            )
        except OSError as failure:
            self.error = f"could not start kilix-amp: {failure}"
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.available():
                self.error = ""
                return True
            time.sleep(0.05)
        self.error = "kilix-amp did not open its control socket"
        return False

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
        self.starting = False
        self.refresh()

    def refresh(self) -> None:
        self.status = self.backend.command("state") or {}
        listing = self.backend.command("playlist") or {}
        self.playlist = [str(i) for i in listing.get("items", [])]
        if self.selected >= len(self.playlist):
            self.selected = max(0, len(self.playlist) - 1)

    def start_backend(self) -> None:
        self.starting = True
        try:
            if self.backend.start():
                self.refresh()
        finally:
            self.starting = False


def render(surface, state: State) -> None:
    available = state.backend.available()
    playing = str(state.status.get("state", "stopped"))
    title = str(state.status.get("title", ""))
    summary = (
        f"{playing} · {title}".rstrip(" ·")
        if available else "kilix-amp backend not running"
    )
    body = shell.draw(
        surface,
        title="Music",
        sections=("Playlist",),
        summary=summary,
        footer=(
            "space play/pause · n next · p prev · r refresh · q quit"
            if available else "s start backend · r retry · q quit"
        ),
        summary_role="alert" if not available or state.backend.error else "muted",
    )
    if not available:
        installed = state.backend.installed()
        shell.put(surface, body.top, body.left,
                  "This front end drives kilix-amp over a control socket;")
        shell.put(surface, body.top + 1, body.left,
                  "no backend is listening yet.")
        if installed:
            shell.put(surface, body.top + 3, body.left,
                      "Press s to start one.")
        else:
            shell.put(surface, body.top + 3, body.left,
                      "kilix-amp is not built here — install the Media Player",
                      shell.tango.attr("alert"))
            shell.put(surface, body.top + 4, body.left,
                      "from the Kilix 95 desktop, then press s.")
        shell.put(surface, body.top + 6, body.left,
                  f"expected socket: {state.backend.path}",
                  shell.tango.attr("muted"))
        if state.backend.error:
            shell.put(surface, body.top + 7, body.left, state.backend.error,
                      shell.tango.attr("alert"))
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
            f"{clock(position)} / {clock(length)}  "
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

    # Opening the player is the request for a player: bring the backend up
    # rather than making the first thing on screen an instruction to.
    if not state.backend.available():
        state.start_backend()

    def handle(key: int, s: State) -> bool:
        if keymap.is_quit(key):
            return False
        if key == ord("s") and not s.backend.available():
            s.start_backend()
        elif key == ord(" "):
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
