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

import argparse
import json
import selectors
import signal
import subprocess
import threading
import time

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_desk import registry  # noqa: E402
from kilix_tui import app, keys as keymap, proc, shell  # noqa: E402
from kilix_tui.music_protocol import MusicControl, PROTOCOL_VERSION, control_parent  # noqa: E402

# Long enough for the backend to open its audio device on a slow disk, short
# enough that a wedged one does not look like a hung UI.
START_TIMEOUT = 5.0
# A first install clones and compiles kilix-amp. This is an upper bound on a
# slow machine, not an expected wait; it runs off the UI thread either way.
INSTALL_TIMEOUT = 900.0
QUERY_TIMEOUT = 5.0


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
    except (TypeError, ValueError, OverflowError):
        total = 0
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def kilix_launcher() -> str:
    """Use the shared selected-host resolver, never an Amp PATH lookup."""
    explicit = os.environ.get("KILIX_HOME")
    if explicit is not None:
        if not os.path.isabs(explicit):
            return ""
        path = os.path.join(explicit, "kilix")
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else ""
    command = registry.kilix_command()
    return command[0] if command else ""


def _root(root):
    if root is None:
        return None
    if not isinstance(root, str) or not os.path.isabs(root) or "\0" in root:
        raise ValueError("Music content root must be an absolute path")
    return os.path.normpath(root)


def _launch_root():
    root = os.environ.get("KILIX_CONTENT_ROOT")
    if root is None and os.environ.get("KILIX95_HOME"):
        raise ValueError("the embedding desktop must pass its actual KILIX_CONTENT_ROOT")
    return _root(root)


def _host_selection(*, install=False, timeout=QUERY_TIMEOUT, cancelled=None, root=None,
                    _deadline=None):
    """One bounded owned host query/setup; all catalog decisions stay there."""
    maximum = INSTALL_TIMEOUT if install else QUERY_TIMEOUT
    if type(timeout) not in (int, float) or not 0 < timeout <= maximum:
        raise ValueError("invalid player setup/query deadline")
    maximum_deadline = time.monotonic() + timeout
    deadline = maximum_deadline if _deadline is None else _deadline
    if type(deadline) not in (int, float) or not deadline <= maximum_deadline:
        raise ValueError("invalid player setup/query deadline")
    if (cancelled and cancelled.is_set()) or time.monotonic() >= deadline:
        raise ValueError("player setup/query canceled")
    root = _root(root)
    launcher = kilix_launcher()
    helper = os.path.join(os.path.dirname(os.path.realpath(launcher)),
                          "scripts", "install-kilix-amp.py") if launcher else ""
    if not helper or not os.path.isfile(helper):
        raise ValueError("selected Kilix host lacks the Amp catalog query; update the host")
    command = [sys.executable, "-I", "-B", helper, "--json" if install else "--resolve"]
    if root is not None:
        command.extend(["--content-root", root])
    process = None
    output, diagnostic = bytearray(), bytearray()
    try:
        # Host/path resolution may yield to cancellation or consume the budget.
        # Keep the caller's authority at the last boundary before side effects.
        if (cancelled and cancelled.is_set()) or time.monotonic() >= deadline:
            raise ValueError("player setup/query canceled or deadline exceeded")
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        with selectors.DefaultSelector() as poller:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                poller.register(stream, selectors.EVENT_READ)
            while poller.get_map() or process.poll() is None:
                if (cancelled and cancelled.is_set()) or time.monotonic() >= deadline:
                    raise ValueError("player setup/query canceled or deadline exceeded")
                for key, _ in poller.select(min(.05, max(0, deadline-time.monotonic()))):
                    block = os.read(key.fd, 4096)
                    if not block:
                        poller.unregister(key.fileobj)
                        continue
                    if key.fileobj is process.stdout:
                        output.extend(block)
                        if len(output) > 32768:
                            raise ValueError("host player selection exceeds its bound")
                    else:
                        diagnostic.extend(block)
                        del diagnostic[:-8192]
        if (cancelled and cancelled.is_set()) or time.monotonic() >= deadline:
            raise ValueError("player setup/query canceled or deadline exceeded")
        if process.returncode != 0:
            detail = diagnostic.decode("utf-8", errors="replace").strip()
            raise ValueError(detail or "selected host refused player setup/query")
        try:
            result = json.loads(output)
        except (RecursionError, UnicodeDecodeError) as failure:
            raise ValueError("malformed host player selection") from failure
        if (not isinstance(result, dict) or result.get("id") != "kilix-amp"
                or _root(result.get("root")) != result.get("root")
                or result.get("root") is None
                or (root is not None and result["root"] != root)
                or not isinstance(result.get("ref"), str) or len(result["ref"]) != 40
                or any(c not in "0123456789abcdef" for c in result["ref"])
                or not isinstance(result.get("build"), list)
                or not all(isinstance(item, str) for item in result["build"])):
            raise ValueError("host player selection does not match the requested catalog/root")
        executable = result.get("executable")
        if executable is not None and (not isinstance(executable, str)
                or not os.path.isabs(executable) or not os.path.isfile(executable)
                or not os.access(executable, os.X_OK)):
            raise ValueError("selected catalog player is not executable")
        if (cancelled and cancelled.is_set()) or time.monotonic() >= deadline:
            raise ValueError("player setup/query canceled or deadline exceeded")
        return result
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    # The host supervisor reaps Content's separate build
                    # sessions before exiting. A cancel ACK is not cleanup.
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=1)
                        raise ValueError("player setup cleanup was not confirmed")
            finally:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


def backend_selection(timeout=QUERY_TIMEOUT, cancelled=None, root=None, *, _deadline=None):
    root = _root(root)
    override = os.environ.get("KILIX_AMP", "")
    if override:
        if cancelled and cancelled.is_set():
            raise ValueError("player startup canceled")
        executable = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(executable) or not os.access(executable, os.X_OK):
            raise ValueError("explicit KILIX_AMP is not built or executable")
        return {"executable": executable, "root": root, "override": True}
    return _host_selection(timeout=timeout, cancelled=cancelled, root=root, _deadline=_deadline)


def backend_executable() -> str:
    """Read-only catalog lookup for explicit callers, never for rendering."""
    try:
        return backend_selection(root=_launch_root())["executable"] or ""
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return ""


def install_backend(timeout: float = INSTALL_TIMEOUT, cancelled: threading.Event | None = None,
                    *, root=None) -> bool:
    """Explicit cancellable setup through the same host catalog and root."""
    try:
        root = _launch_root() if root is None else _root(root)
        return bool(_host_selection(install=True, timeout=timeout, cancelled=cancelled,
                                    root=root)["executable"])
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False

class Backend(MusicControl):
    """An authenticated local client, owning only a backend it starts itself."""

    def __init__(self, path: str | None = None) -> None:
        super().__init__(path or socket_path())
        self._owned: subprocess.Popen | None = None
        self.selection = {}

    def installed(self) -> bool:
        # Rendering reads only the last explicit worker lookup.
        return bool(self.selection.get("executable"))

    def select(self, *, timeout=QUERY_TIMEOUT, cancelled=None, root=None, _deadline=None):
        self.selection = backend_selection(timeout, cancelled, root, _deadline=_deadline)
        return self.selection

    def start(self, timeout: float = START_TIMEOUT) -> bool:
        if type(timeout) not in (float, int) or not 0 < timeout <= START_TIMEOUT:
            self.error = "invalid backend startup deadline"
            return False
        if self._closed.is_set():
            return False
        deadline = time.monotonic() + timeout
        if os.path.lexists(self.path):
            if not self.negotiate(timeout=timeout, _deadline=deadline):
                return False
            with self._connection_lock:
                try:
                    self._check_active(deadline)
                except (OSError, ValueError) as failure:
                    self.identity = None
                    self.version = None
                    self.capabilities = {}
                    self.error = str(failure)
                    return False
                return True
        try:
            root = self.selection["root"] if "root" in self.selection else _launch_root()
            selected = self.select(timeout=timeout, cancelled=self._closed, root=root,
                                   _deadline=deadline)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as failure:
            self.error = str(failure)
            return False
        executable = selected["executable"]
        if not executable:
            self.error = "the selected catalog kilix-amp is not built; request setup"
            return False
        try:
            with control_parent(self.path, create=True):
                pass
        except (OSError, ValueError) as failure:
            self.error = f"cannot create a private player endpoint: {failure}"
            return False
        if self._closed.is_set() or time.monotonic() >= deadline:
            self.error = "player startup canceled or deadline exceeded"
            return False
        command = [executable, "--headless", "--socket", self.path]
        music = os.path.expanduser("~/Music")
        if os.path.isdir(music):
            command.append(music)
        try:
            environment = dict(os.environ)
            if selected["root"] is not None:
                environment["KILIX_CONTENT_ROOT"] = selected["root"]
            self._owned = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, env=environment)
        except OSError as failure:
            self.error = f"could not start kilix-amp: {failure}"
            return False
        child = self._owned
        while time.monotonic() < deadline and not self._closed.is_set():
            if child.poll() is not None:
                break
            if self.available() and self.negotiate(timeout=timeout, _deadline=deadline):
                with self._connection_lock:
                    try:
                        self._check_active(deadline)
                    except (OSError, ValueError) as failure:
                        self.identity = None
                        self.version = None
                        self.capabilities = {}
                        self.error = str(failure)
                        break
                    if (self._owned is child and child.poll() is None
                            and self.identity and self.identity[-1] == child.pid):
                        return True
                    self.error = "another backend replaced the startup endpoint"
                    break
            self._closed.wait(.05)
        failure = self.error or "kilix-amp did not open a healthy control socket"
        self._stop_owned()
        self.error = failure
        return False

    def close(self) -> None:
        """Never send quit or a signal to an attached, pre-existing backend."""
        identity, version = self._close_control()
        self._stop_owned(identity=identity, version=version)

    def _stop_owned(self, *, identity=None, version=None) -> None:
        child = self._owned
        if child is None:
            return
        if identity is None:
            identity, version = self.identity, self.version
        try:
            if child.poll() is None:
                if identity and identity[-1] == child.pid:
                    try:
                        self._exchange("quit", version, expected=identity, timeout=.5, closing=True)
                    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                        pass
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
        except (OSError, subprocess.SubprocessError) as failure:
            self.error = f"could not confirm owned player exit: {failure}"
            return
        if self._owned is child and child.poll() is not None:
            self._owned = None
            with self._connection_lock:
                if self.identity and self.identity[-1] == child.pid:
                    self.identity = None
                    self.version = None
                    self.capabilities = {}


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
        self.filter = shell.Filter()
        self._worker: threading.Thread | None = None
        self._closing = threading.Event()
        self.prompt_kind = "add"

    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def typing(self) -> bool:
        return self.prompt is not None or self.filter.typing

    def view(self) -> list[tuple[int, str]]:
        """The playlist rows on screen, as (playlist index, path).

        The index travels with the row because the backend addresses tracks by
        their position in the *whole* playlist: filtering renumbers what is on
        screen, and playing row 0 of a filtered list must not play track 0.
        """
        pairs = list(enumerate(self.playlist))
        if not self.filter.text:
            return pairs
        return [pair for pair in pairs
                if self.filter.matches(os.path.basename(pair[1]))]

    def absorb(self, reply: dict) -> None:
        """Take the status a mutating command already returned."""
        if reply and "state" in reply:
            self.status = reply

    def refresh(self) -> None:
        self.status = self.backend.command("state") or {}
        if not self.status or self.status.get("ok") is False:
            self.message = safe_text(self.backend.error or str(self.status.get("error") or "player unavailable"))
            self.status = {}; self.playlist = []
            return
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
            self._work(lambda: self.absorb(self.backend.command("state")))
            return
        self._work(self.refresh)

    def _work(self, action) -> None:
        if self.busy() or self._closing.is_set():
            return
        def run():
            try:
                if not self._closing.is_set():
                    action()
            except (OSError, ValueError, TypeError, OverflowError) as failure:
                self.message = safe_text(str(failure))
        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    # ── transport, as the backend defines it ────────────────────────────────

    def send(self, name: str, **fields: object) -> None:
        def request():
            reply = self.backend.command(name, **fields)
            self.absorb(reply)
            if not reply or reply.get("ok") is False:
                self.message = safe_text(self.backend.error or str(reply.get("error") or "player command failed"))
            elif name == "shuffle":
                self.message = "shuffle on" if reply.get("shuffle") else "shuffle off"
            elif name == "repeat":
                self.message = REPEAT_LABELS.get(self.repeat_mode(), "repeat")
            elif name == "clear":
                self.refresh()
                self.message = "playlist cleared"
            elif name == "volume":
                self.message = f"volume {self.volume()}%"
        self._work(request)

    def position(self) -> float:
        return float(self.status.get("pos", 0) or 0)

    def length(self) -> float:
        return float(self.status.get("len", 0) or 0)

    def volume(self) -> int:
        return number(self.status, "volume", 0)

    def repeat_mode(self) -> int:
        return number(self.status, "repeat", 0)

    def seek_by(self, delta: float) -> None:
        if self.status.get("live") or self.status.get("seekable") is False:
            self.message = "this source cannot seek"
            return
        length = self.length()
        if length <= 0:
            self.message = "nothing playing to seek in"
            return
        target = min(max(0.0, self.position() + delta), max(0.0, length - 0.5))
        self.send("seek", pos=round(target, 2))

    def nudge_volume(self, delta: int) -> None:
        level = max(0, min(100, self.volume() + delta))
        self.send("volume", level=level)

    def add_path(self, raw: str) -> None:
        path = os.path.expanduser(raw.strip())
        if not path:
            return
        def add():
            reply = self.backend.command("add", path=path)
            self.absorb(reply)
            if not reply or reply.get("ok") is False:
                self.message = safe_text(self.backend.error or str(reply.get("error") or "could not add that path"))
            else:
                self.refresh()
                self.message = f"added {safe_text(os.path.basename(path.rstrip('/')) or path)}"
        self._work(add)

    def open_source(self, kind: str, raw: str) -> None:
        path = os.path.abspath(os.path.expanduser(raw.strip())) if raw.strip() else ""
        if not path:
            return
        def opened():
            reply = self.backend.command("open", source_type=kind, path=path)
            self.absorb(reply)
            if not reply or reply.get("ok") is False:
                self.message = safe_text(self.backend.error or str(reply.get("error") or "could not open source"))
            else:
                self.refresh()
        self._work(opened)

    def begin_setup(self, source: tuple[str, str] | None = None) -> None:
        """Install the player if needed, then start a backend, off this thread."""
        if self.busy():
            return
        self.note = ""
        self._work(lambda: self._setup(source))

    def _setup(self, source: tuple[str, str] | None = None) -> None:
        try:
            if not os.path.lexists(self.backend.path):
                self.phase = "checking"
                selected = self.backend.select(cancelled=self._closing,
                    root=_launch_root())
                if not selected["executable"]:
                    self.phase = "installing"
                    if not install_backend(cancelled=self._closing, root=selected["root"]):
                        self.note = "could not install the selected Media Player; check host setup prerequisites"
                        return
            self.phase = "starting"
            if not self.backend.start():
                self.note = self.backend.error
                return
            if source and not self._closing.is_set():
                kind, path = source
                reply = self.backend.command("open", source_type=kind, path=path)
                self.absorb(reply)
                if not reply or reply.get("ok") is False:
                    self.note = self.backend.error
            self.refresh()
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as failure:
            self.note = safe_text(str(failure))
        finally:
            self.phase = ""

    def close(self) -> None:
        self._closing.set()
        self.backend.close()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=6)


WAITING = {
    "checking": "checking the selected Media Player",
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
    except (TypeError, ValueError, OverflowError):
        return default


def safe_text(value: str) -> str:
    return "".join(" " if ord(c) < 32 or 127 <= ord(c) < 160 else c for c in value)


SECTIONS = ("Now playing", "Playlist")
REPEAT_LABELS = {0: "repeat off", 1: "repeat all", 2: "repeat one"}
STATE_GLYPH = {"playing": "▶", "paused": "❚❚", "stopped": "■"}
SEEK_STEP = 5.0
SEEK_JUMP = 30.0
VOLUME_STEP = 5


def footer(state: State) -> str:
    if state.filter.typing:
        return state.filter.footer()
    if state.prompt is not None:
        kind = state.prompt_kind
        return ("type a private socket path · Enter open · Esc cancel" if kind == "encodec-unix"
                else "type a file path · Enter open · Esc cancel" if kind == "file"
                else "type a file or folder · Enter add · Esc cancel")
    if state.phase:
        return "q quit"
    if not state.backend.available():
        return "s start backend · r retry · q quit"
    if state.section == 1:
        return ("↑/↓ select · Enter play · / filter · a add · c clear "
                "· o file · u stream · Tab now playing · ? keys · q quit")
    if state.status.get("live"):
        reconnect = (state.status.get("source_type") == "encodec-unix"
                     and (state.status.get("ended") or state.status.get("reconnect_required")))
        return ("space play/pause · v stop · +/- volume · u stream · r "
                + ("reconnect" if reconnect else "refresh") + " · Tab playlist · ? keys · q quit")
    return ("space play/pause · ←/→ seek · +/- volume · z/b prev/next "
            "· o file · u stream · Tab playlist · ? keys · q quit")


def _draw_now_playing(surface, state: State, body) -> None:
    status = state.status
    playing = str(status.get("state", "stopped"))
    glyph = STATE_GLYPH.get(playing, "·")
    title = safe_text(str(status.get("title") or ""))
    path = safe_text(str(status.get("file") or ""))
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
    if status.get("live"):
        state_name = ("reconnect required" if status.get("reconnect_required") else
                      "ended" if status.get("ended") and playing == "stopped" else
                      "recovering" if status.get("degraded") else playing)
        shell.put(surface, row, body.left, f"LIVE · {clock(position)} elapsed · {state_name}",
                  shell.tango.attr("alert" if status.get("reconnect_required") else "accent"))
    elif length > 0:
        times = f"{clock(position)} / {clock(length)}"
        width = max(4, body.width - len(times) - 4)
        shell.put(surface, row, body.left, times, shell.tango.attr("accent"))
        shell.put(surface, row, body.left + len(times) + 2,
                  proc.bar(position / length, width),
                  shell.tango.attr("accent"))
    else:
        shell.put(surface, row, body.left, "duration unavailable" if path else "no track loaded",
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
    if status.get("codec") == "encodec":
        row += 2
        profile = {1: "24 kHz mono", 2: "48 kHz stereo"}.get(status.get("profile"), "profile pending")
        rate = number(status, "bitrate")
        detail = f"EnCodec · {profile}"
        if rate:
            detail += f" · {rate // 1000} kb/s"
        if number(status, "threads"):
            detail += f" · {number(status, 'threads')} threads"
        shell.put(surface, row, body.left, detail[:body.width], shell.tango.attr("accent"))
        row += 1
        readiness = "model ready" if status.get("model_ready") else "model not ready"
        if status.get("buffering"):
            readiness += " · buffering"
        readiness += " · seekable" if status.get("seekable") else " · seek unavailable"
        shell.put(surface, row, body.left, readiness[:body.width], shell.tango.attr("muted"))
    if status.get("source_error_message"):
        shell.put(surface, row + 2, body.left, safe_text(status["source_error_message"])[:body.width],
                  shell.tango.attr("alert"))


def _draw_playlist(surface, state: State, body) -> None:
    rows = state.view()
    if not rows:
        message = ("nothing matches that filter" if state.filter.text
                   else "the playlist is empty — press a to add a file or folder")
        shell.put(surface, body.top, body.left, message,
                  shell.tango.attr("muted"))
        return
    current = number(state.status, "index", -1)
    state.selected = max(0, min(state.selected, len(rows) - 1))
    height = max(1, body.height - (2 if state.typing() else 0))
    first = visible_window(len(rows), height, state.selected)
    for line in range(min(height, len(rows) - first)):
        position = first + line
        index, item = rows[position]
        selected = position == state.selected
        # Two different meanings, two different marks: the cursor is where you
        # are, the note is what the backend is actually playing.
        cursor = "▶" if selected else " "
        playing = "♪" if index == current else " "
        label = f" {cursor}{playing} {safe_text(os.path.basename(item))}"
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
    elif state.section == 1 and state.filter.active():
        summary = state.filter.summary(len(state.view()))
    elif state.message:
        summary = safe_text(state.message)
    elif available:
        summary = f"{state.status.get('state', 'stopped')}"
        if state.backend.error:
            summary = safe_text(state.backend.error)
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
    if state.prompt is not None:
        label = {"add": "add", "file": "file", "encodec-unix": "stream"}[state.prompt_kind]
        shell.put(surface, body.bottom - 1, body.left,
                  f"{label}: {safe_text(state.prompt)}_"[:body.width],
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
    else:
        shell.put(surface, body.top + 3, body.left,
                  "Press s to prepare the selected Media Player and start it.")
    shell.put(surface, body.top + 6, body.left,
              f"expected socket: {safe_text(state.backend.path)}",
              shell.tango.attr("muted"))
    for offset, message in enumerate((state.note, state.backend.error)):
        if message:
            shell.put(surface, body.top + 7 + offset, body.left, safe_text(message),
                      shell.tango.attr("alert"))


def _typed(key: int, state: State) -> bool:
    """Keys while a path is being entered. Everything printable is text."""
    if key == 27:                                   # Esc
        state.prompt = None
    elif key in (ord("\n"), ord("\r")):
        if state.busy() or state._closing.is_set():
            state.message = "player busy; press Enter again when ready"
            return True
        state.message = ""
        entry, state.prompt = state.prompt or "", None
        if state.prompt_kind == "add":
            state.add_path(entry)
        else:
            state.open_source(state.prompt_kind, entry)
    elif key in keymap.BACKSPACE:
        state.prompt = (state.prompt or "")[:-1]
    elif keymap.is_text(key):
        candidate = (state.prompt or "") + chr(key)
        if len(candidate.encode("utf-8")) < 4096:
            state.prompt = candidate
        else:
            state.message = "path is too long"
    return True


def handle(key: int, state: State) -> bool:
    if state.help_open:
        state.help_open = False
        return True
    if state.filter.typing:
        state.filter.handle(key)
        return True
    if state.prompt is not None:
        return _typed(key, state)
    if keymap.is_quit(key):
        return False
    if state.busy():
        return True              # an install is running; ignore transport keys
    state.message = ""
    if key == ord("?"):
        state.help_open = True
        return True
    if state.section == 1 and state.filter.handle(key):     # `/` filters
        state.selected = 0
        return True
    if not state.backend.available():
        if key == ord("s"):
            state.begin_setup()
        elif keymap.is_refresh(key):
            state._work(state.refresh)
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
    elif key == ord("m"):
        state.send("repeat")
    elif key == ord("a"):
        state.prompt = ""; state.prompt_kind = "add"
    elif key == ord("o"):
        state.prompt = ""; state.prompt_kind = "file"
    elif key == ord("u"):
        state.prompt = ""; state.prompt_kind = "encodec-unix"
    elif key == ord("c"):
        state.send("clear")
    elif (step := keymap.direction(key)) and state.section == 1:
        rows = state.view()
        state.selected = max(0, min(len(rows) - 1, state.selected + step))
    elif key in (ord("\n"), ord("\r")) and state.section == 1:
        rows = state.view()
        if rows:
            # The playlist index, not the row number on screen.
            state.send("play", index=rows[state.selected][0])
    elif keymap.is_refresh(key):
        if state.status.get("source_type") == "encodec-unix" and (state.status.get("reconnect_required") or state.status.get("ended")):
            state.open_source("encodec-unix", state.status.get("file", ""))
        else:
            state._work(state.refresh)
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
        # Not named `handle`: that is this module's key handler, and binding it
        # here would make the name local to `main` and unbound at `app.run`.
        with open(path, "w", encoding="utf-8") as out:
            out.write(app.render_to_text(render, state) + "\n")
        state.close()
        return 0

    parser = argparse.ArgumentParser(description="Control the shared Kilix Amp player")
    parser.add_argument("file", nargs="?", help="open a file with a protocol-2 backend")
    parser.add_argument("--encodec-socket", help="open a framed EnCodec stream from a private Unix socket")
    options = parser.parse_args(argv)
    if options.file and options.encodec_socket:
        parser.error("choose one file or live socket source")
    source = None
    if options.file or options.encodec_socket:
        source = ("encodec-unix" if options.encodec_socket else "file",
                  os.path.abspath(os.path.expanduser(options.encodec_socket or options.file)))

    # Opening the player is the request for a player: bring the backend up —
    # building it first if this machine never has — rather than making the
    # first thing on screen an instruction to.
    if source or not state.backend.available():
        state.begin_setup(source)

    try:
        return app.run(render, state, handle=handle, tick_ms=1000, help_key=False)
    finally:
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
