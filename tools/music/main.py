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
    """Everything on screen, and the one place that talks to the backend.

    `status` is whatever the last reply carried, so the renderer never has to
    ask a second time: every mutating command answers with the state it
    produced, which is why a keypress redraws correctly without a round trip
    of its own.
    """

    def __init__(self) -> None:
        self.backend = Backend()
        self.status: dict = {}
        self.playlist: list[str] = []
        self.section = 0            # 0 Now playing, 1 Playlist
        self.selected = 0
        self.phase = ""             # "", "installing", "starting"
        self.note = ""              # why the last attempt did not get there
        self.message = ""           # one line of feedback about the last key
        self.help_open = False
        self.prompt: str | None = None   # the "add path" entry, when open
        self._worker: threading.Thread | None = None
        self.refresh()

    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def typing(self) -> bool:
        return self.prompt is not None

    def absorb(self, reply: dict) -> None:
        """Take the status a mutating command already returned."""
        if reply and "state" in reply:
            self.status = reply

    def refresh(self) -> None:
        self.status = self.backend.command("state") or {}
        listing = self.backend.command("playlist") or {}
        self.playlist = [str(i) for i in listing.get("items", [])]
        if self.selected >= len(self.playlist):
            self.selected = max(0, len(self.playlist) - 1)

    def tick(self) -> None:
        """Pull current state for a redraw.

        The loop wakes on its own timer without calling `handle`, so a playing
        position only advances if the draw path asks for it. While a path is
        being typed the playlist is left alone: re-reading it under the cursor
        would move the entry the user is looking at.
        """
        if self.busy() or not self.backend.available():
            return
        if self.typing():
            self.status = self.backend.command("state") or {}
            return
        self.refresh()

    # ── transport, as the backend defines it ────────────────────────────────

    def send(self, name: str, **fields: object) -> None:
        self.absorb(self.backend.command(name, **fields))

    def position(self) -> float:
        return float(self.status.get("pos", 0) or 0)

    def length(self) -> float:
        return float(self.status.get("len", 0) or 0)

    def volume(self) -> int:
        return number(self.status, "volume", 0)

    def repeat_mode(self) -> int:
        return number(self.status, "repeat", 0)

    def seek_by(self, delta: float) -> None:
        length = self.length()
        if length <= 0:
            self.message = "nothing playing to seek in"
            return
        target = min(max(0.0, self.position() + delta), max(0.0, length - 0.5))
        self.send("seek", pos=round(target, 2))

    def nudge_volume(self, delta: int) -> None:
        level = max(0, min(100, self.volume() + delta))
        self.send("volume", level=level)
        self.message = f"volume {level}%"

    def add_path(self, raw: str) -> None:
        path = os.path.expanduser(raw.strip())
        if not path:
            return
        reply = self.backend.command("add", path=path)
        self.absorb(reply)
        if reply.get("ok") is False:
            self.message = str(reply.get("error") or "could not add that path")
        else:
            self.refresh()
            self.message = f"added {os.path.basename(path.rstrip('/')) or path}"

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

def number(status: dict, key: str, default: int = 0) -> int:
    """An integer field from a status reply, where 0 is a real value.

    `status.get(key) or default` reads naturally and is wrong here: track index
    0 is the first track, and `0 or -1` is -1, which hid the ♪ marker and the
    "track 1 of N" line for whatever was playing first.
    """
    value = status.get(key, default)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


SECTIONS = ("Now playing", "Playlist")
REPEAT_LABELS = {0: "repeat off", 1: "repeat all", 2: "repeat one"}
STATE_GLYPH = {"playing": "▶", "paused": "❚❚", "stopped": "■"}
SEEK_STEP = 5.0
SEEK_JUMP = 30.0
VOLUME_STEP = 5


def footer(state: State) -> str:
    if state.typing():
        return "type a file or folder · Enter add · Esc cancel"
    if state.phase:
        return "q quit"
    if not state.backend.available():
        return "s start backend · r retry · q quit"
    if state.section == 1:
        return ("↑/↓ select · Enter play · a add · c clear · space play/pause "
                "· Tab now playing · ? keys · q quit")
    return ("space play/pause · ←/→ seek · +/- volume · z/b prev/next "
            "· s shuffle · m repeat · Tab playlist · ? keys · q quit")


def _draw_now_playing(surface, state: State, body) -> None:
    status = state.status
    playing = str(status.get("state", "stopped"))
    glyph = STATE_GLYPH.get(playing, "·")
    title = str(status.get("title") or "")
    path = str(status.get("file") or "")
    if not title:
        title = os.path.basename(path) or "nothing loaded"
    index = number(status, "index", -1)
    count = number(status, "count", 0)

    row = body.top
    shell.put(surface, row, body.left, f"{glyph}  {title}"[:body.width],
              shell.tango.attr("title"))
    row += 1
    if path:
        shell.put(surface, row, body.left + 3,
                  os.path.dirname(path)[:max(0, body.width - 3)],
                  shell.tango.attr("muted"))
    row += 2

    length = state.length()
    position = state.position()
    if length > 0:
        times = f"{clock(position)} / {clock(length)}"
        width = max(4, body.width - len(times) - 4)
        shell.put(surface, row, body.left, times, shell.tango.attr("accent"))
        shell.put(surface, row, body.left + len(times) + 2,
                  proc.bar(position / length, width),
                  shell.tango.attr("accent"))
    else:
        shell.put(surface, row, body.left, "no track loaded",
                  shell.tango.attr("muted"))
    row += 2

    # Volume, shuffle and repeat all arrive in the same status reply, so the
    # front end shows them rather than making the user guess what is set.
    level = state.volume()
    meter = shell.meter(level / 100.0, max(4, min(20, body.width - 24)))
    shell.put(surface, row, body.left, f"vol  {meter} {level:3d}%",
              shell.tango.attr("accent" if level else "muted"))
    row += 1
    shuffle_on = bool(status.get("shuffle"))
    repeat = state.repeat_mode()
    shell.put(surface, row, body.left,
              "shuffle on" if shuffle_on else "shuffle off",
              shell.tango.attr("accent" if shuffle_on else "muted"))
    shell.put(surface, row, body.left + 14, REPEAT_LABELS.get(repeat, "repeat"),
              shell.tango.attr("accent" if repeat else "muted"))
    if count:
        shell.put(surface, row, body.left + 30,
                  f"track {index + 1} of {count}" if index >= 0
                  else f"{count} in playlist", shell.tango.attr("muted"))


def _draw_playlist(surface, state: State, body) -> None:
    if not state.playlist:
        shell.put(surface, body.top, body.left,
                  "the playlist is empty — press a to add a file or folder",
                  shell.tango.attr("muted"))
        return
    current = number(state.status, "index", -1)
    state.selected = max(0, min(state.selected, len(state.playlist) - 1))
    height = max(1, body.height - (2 if state.typing() else 0))
    first = visible_window(len(state.playlist), height, state.selected)
    for line in range(min(height, len(state.playlist) - first)):
        index = first + line
        item = state.playlist[index]
        selected = index == state.selected
        # Two different meanings, two different marks: the cursor is where you
        # are, the note is what the backend is actually playing.
        cursor = "▶" if selected else " "
        playing = "♪" if index == current else " "
        label = f" {cursor}{playing} {os.path.basename(item)}"
        if selected:
            shell.put(surface, body.top + line, 0,
                      label.ljust(body.width + 1)[:body.width + 1],
                      shell.tango.attr("selected"))
        else:
            shell.put(surface, body.top + line, 0, label[:body.width + 1],
                      shell.tango.attr("accent") if index == current else 0)


def visible_window(count: int, height: int, selected: int) -> int:
    """The first visible index, keeping the selection on screen."""
    if height <= 0 or count <= height:
        return 0
    return max(0, min(selected - height + 1, count - height))


def render(surface, state: State) -> None:
    state.tick()
    available = state.backend.available()
    waiting = WAITING.get(state.phase, "")
    if waiting:
        summary = waiting
    elif state.message:
        summary = state.message
    elif available:
        summary = f"{state.status.get('state', 'stopped')}"
        if state.backend.error:
            summary = state.backend.error
    else:
        summary = "kilix-amp backend not running"
    body = shell.draw(
        surface,
        title="Music",
        sections=SECTIONS,
        active=state.section,
        summary=summary,
        footer=footer(state),
        help_key=False,          # this tool owns `?`, so a typed path may use it
        summary_role=(
            "alert" if (not available or state.backend.error) and not waiting
            else "accent" if state.message else "muted"),
    )
    if waiting:
        shell.put(surface, body.top, body.left, waiting.capitalize())
        shell.put(surface, body.top + 2, body.left,
                  "The player is built once, from the commit Kilix pins.",
                  shell.tango.attr("muted"))
        return
    if not available:
        _draw_offline(surface, state, body)
        return
    if state.section == 0:
        _draw_now_playing(surface, state, body)
    else:
        _draw_playlist(surface, state, body)
    if state.typing():
        shell.put(surface, body.bottom - 1, body.left,
                  f"add: {state.prompt}_"[:body.width],
                  shell.tango.attr("selected"))
    if state.help_open:
        app.help_overlay(surface)


def _draw_offline(surface, state: State, body) -> None:
    shell.put(surface, body.top, body.left,
              "This front end drives kilix-amp over a control socket;")
    shell.put(surface, body.top + 1, body.left,
              "no backend is listening yet.")
    if state.backend.installed():
        shell.put(surface, body.top + 3, body.left, "Press s to start one.")
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


def _typed(key: int, state: State) -> bool:
    """Keys while a path is being entered. Everything printable is text."""
    if key == 27:                                   # Esc
        state.prompt = None
    elif key in (ord("\n"), ord("\r")):
        entry, state.prompt = state.prompt or "", None
        state.add_path(entry)
    elif key in keymap.BACKSPACE:
        state.prompt = (state.prompt or "")[:-1]
    elif keymap.is_text(key):
        state.prompt = (state.prompt or "") + chr(key)
    return True


def handle(key: int, state: State) -> bool:
    if state.help_open:
        state.help_open = False
        return True
    if state.typing():
        return _typed(key, state)
    if keymap.is_quit(key):
        return False
    if state.busy():
        return True              # an install is running; ignore transport keys
    state.message = ""
    if key == ord("?"):
        state.help_open = True
        return True
    if not state.backend.available():
        if key == ord("s"):
            state.begin_setup()
        elif keymap.is_refresh(key):
            state.refresh()
        return True
    if key == ord("\t"):
        state.section = (state.section + 1) % len(SECTIONS)
    elif ord("1") <= key <= ord("0") + len(SECTIONS):
        state.section = key - ord("1")
    elif key == ord(" "):
        state.send("toggle")
    elif key in (ord("b"), ord("n")):
        state.send("next")
    elif key in (ord("z"), ord("p")):
        state.send("previous")
    elif key == ord("v"):
        state.send("stop")
    elif key in keymap.LEFT and state.section == 0:
        state.seek_by(-SEEK_STEP)
    elif key in keymap.RIGHT and state.section == 0:
        state.seek_by(SEEK_STEP)
    elif key == ord(","):
        state.seek_by(-SEEK_JUMP)
    elif key == ord("."):
        state.seek_by(SEEK_JUMP)
    elif key in (ord("+"), ord("=")):
        state.nudge_volume(VOLUME_STEP)
    elif key == ord("-"):
        state.nudge_volume(-VOLUME_STEP)
    elif key == ord("s"):
        state.send("shuffle")
        state.message = ("shuffle on" if state.status.get("shuffle")
                         else "shuffle off")
    elif key == ord("m"):
        state.send("repeat")
        state.message = REPEAT_LABELS.get(state.repeat_mode(), "repeat")
    elif key == ord("a"):
        state.prompt = ""
    elif key == ord("c"):
        state.send("clear")
        state.refresh()
        state.message = "playlist cleared"
    elif (step := keymap.direction(key)) and state.section == 1:
        state.selected = max(
            0, min(len(state.playlist) - 1, state.selected + step))
    elif key in (ord("\n"), ord("\r")) and state.section == 1:
        if state.playlist:
            state.send("play", index=state.selected)
    elif keymap.is_refresh(key):
        state.refresh()
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
        # Not named `handle`: that is this module's key handler, and binding it
        # here would make the name local to `main` and unbound at `app.run`.
        with open(path, "w", encoding="utf-8") as out:
            out.write(app.render_to_text(render, state) + "\n")
        return 0

    # Opening the player is the request for a player: bring the backend up —
    # building it first if this machine never has — rather than making the
    # first thing on screen an instruction to.
    if not state.backend.available():
        state.begin_setup()

    return app.run(render, state, handle=handle, tick_ms=1000, help_key=False)


if __name__ == "__main__":
    raise SystemExit(main())
