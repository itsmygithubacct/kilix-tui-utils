"""kilix-music — a player driving kilix-amp over its control socket.

kilix-amp owns decoding and mixing; this is only a front end, so there is one
playback implementation rather than two that drift. The protocol is a versioned
contract between two repositories: this client declares the version it speaks
and reports a mismatch rather than guessing.

kilix-amp ships the headless backend, so this starts one when none is running
and drives it. Opening this tool is the activation that installs the player:
where kilix-amp was never built, Kilix builds the pinned one first, the same
way the Kilix 95 Media Player always has. Neither step blocks the loop — a
first install compiles an SDL application, and a UI that stops redrawing for
that reads as a hang.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
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
# A first install clones and compiles kilix-amp. This is an upper bound on a
# slow machine, not an expected wait; it runs off the UI thread either way.
INSTALL_TIMEOUT = 900.0


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


def kilix_launcher() -> str:
    """The `kilix` command, or "" when this is not running under Kilix."""
    found = shutil.which("kilix")
    if found:
        return found
    candidate = os.path.join(sources.component_dir("kilix"), "kilix")
    return candidate if os.access(candidate, os.X_OK) else ""


def install_backend(timeout: float = INSTALL_TIMEOUT) -> bool:
    """Build the pinned Media Player, through Kilix rather than around it.

    Kilix owns the content catalog that pins kilix-amp and the installer that
    verifies and builds it. Cloning it from here would mean a second, unpinned
    copy of that decision.
    """
    launcher = kilix_launcher()
    if not launcher:
        return False
    try:
        completed = subprocess.run(
            [launcher, "amp", "--install-only"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(backend_executable())


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
        self.phase = ""    # "", "installing", "starting"
        self.note = ""     # why the last attempt did not get there
        self._worker: threading.Thread | None = None
        self.refresh()

    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def refresh(self) -> None:
        self.status = self.backend.command("state") or {}
        listing = self.backend.command("playlist") or {}
        self.playlist = [str(i) for i in listing.get("items", [])]
        if self.selected >= len(self.playlist):
            self.selected = max(0, len(self.playlist) - 1)

    def tick(self) -> None:
        """Pull current state for a redraw.

        The loop wakes on its own timer without calling `handle`, so a playing
        position only advances if the draw path asks for it.
        """
        if self.busy() or not self.backend.available():
            return
        self.refresh()

    def begin_setup(self) -> None:
        """Install the player if needed, then start a backend, off this thread."""
        if self.busy():
            return
        self.note = ""
        self._worker = threading.Thread(target=self._setup, daemon=True)
        self._worker.start()

    def _setup(self) -> None:
        try:
            if not backend_executable():
                self.phase = "installing"
                if not install_backend():
                    self.note = ("could not install the Media Player — "
                                 "run 'kilix amp' to see why")
                    return
            self.phase = "starting"
            if not self.backend.start():
                self.note = self.backend.error
        finally:
            self.phase = ""


WAITING = {
    "installing": "building the Media Player — this takes a few minutes",
    "starting": "starting kilix-amp…",
}


def render(surface, state: State) -> None:
    state.tick()
    available = state.backend.available()
    playing = str(state.status.get("state", "stopped"))
    title = str(state.status.get("title", ""))
    waiting = WAITING.get(state.phase, "")
    summary = (
        waiting if waiting
        else f"{playing} · {title}".rstrip(" ·") if available
        else "kilix-amp backend not running"
    )
    body = shell.draw(
        surface,
        title="Music",
        sections=("Playlist",),
        summary=summary,
        footer=(
            "q quit" if waiting
            else "space play/pause · n next · p prev · r refresh · q quit"
            if available else "s start backend · r retry · q quit"
        ),
        summary_role=(
            "muted" if available and not state.backend.error and not waiting
            else "alert"),
    )
    if waiting:
        shell.put(surface, body.top, body.left, f"{waiting.capitalize()}")
        shell.put(surface, body.top + 2, body.left,
                  "The player is built once, from the commit Kilix pins.",
                  shell.tango.attr("muted"))
        return
    if not available:
        shell.put(surface, body.top, body.left,
                  "This front end drives kilix-amp over a control socket;")
        shell.put(surface, body.top + 1, body.left,
                  "no backend is listening yet.")
        if state.backend.installed():
            shell.put(surface, body.top + 3, body.left,
                      "Press s to start one.")
        elif kilix_launcher():
            shell.put(surface, body.top + 3, body.left,
                      "Press s to build the pinned Media Player and start it.")
        else:
            shell.put(surface, body.top + 3, body.left,
                      "kilix-amp is not built, and no kilix command was found",
                      shell.tango.attr("alert"))
            shell.put(surface, body.top + 4, body.left,
                      "to build it with. Run 'kilix amp' from a Kilix checkout.")
        shell.put(surface, body.top + 6, body.left,
                  f"expected socket: {state.backend.path}",
                  shell.tango.attr("muted"))
        for offset, message in enumerate((state.note, state.backend.error)):
            if message:
                shell.put(surface, body.top + 7 + offset, body.left, message,
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

    # Opening the player is the request for a player: bring the backend up —
    # building it first if this machine never has — rather than making the
    # first thing on screen an instruction to.
    if not state.backend.available():
        state.begin_setup()

    def handle(key: int, s: State) -> bool:
        if keymap.is_quit(key):
            return False
        if s.busy():
            return True          # an install is running; ignore transport keys
        if key == ord("s") and not s.backend.available():
            s.begin_setup()
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
