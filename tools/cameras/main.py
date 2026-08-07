"""kilix-cameras — camera views and stream profiles for kilix-rtsp.

The desktop's way into the cameras: one list of what is configured, Enter on a
camera fills the window with it, Enter on a group composes a mosaic, and `n`
writes a new stream profile. Viewing is kilix-rtsp's own `view` and `mosaic`
commands handed the terminal — this tool is a menu and a config routine, not a
second decoder.

Two rules come straight from kilix-rtsp and are not stylistic:

* The config file is a secret. RTSP URLs embed credentials, so cameras.conf
  is written mode 0600 under ~/.local/gpu_terminal/kilix-rtsp/, never into a
  checkout, and this tool never prints a URL it read back — the list shows
  names and which tiers exist, nothing else.
* Tiers are never synthesized from one another. A profile carries the main
  and sub URLs the operator typed; deriving one from the other is wrong for
  most device families and silently wrong for the rest.
"""
from __future__ import annotations

import curses
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_desk import sources  # noqa: E402
from kilix_tui import app, keys as keymap, shell  # noqa: E402

# The parser these limits mirror is kilix-rtsp's krtsp_config.c: a profile
# that exceeds them writes a file the next `kilix-rtsp` run refuses.
NAME_MAX = 63            # KRTSP_NAME_MAX - 1
URL_MAX = 511           # KRTSP_URL_MAX - 1
CAMERAS_MAX = 32        # KRTSP_CAMERAS_MAX


def config_root() -> str:
    """The kilix-rtsp home: KILIX_RTSP_HOME, else the fleet's usual root."""
    override = os.environ.get("KILIX_RTSP_HOME", "")
    if override:
        return override
    return os.path.join(
        os.path.expanduser("~"), ".local", "gpu_terminal", "kilix-rtsp")


def config_path() -> str:
    return os.path.join(config_root(), "config", "cameras.conf")


def kilix_rtsp_command() -> list[str] | None:
    """The kilix-rtsp binary: the installed command wins, a checkout last.

    This is the registry's own discipline applied to a sibling project — PATH
    first, the conventional `make install PREFIX=~/.local` destination second,
    and a built source checkout only when nothing installed answers, so a
    checkout can never shadow what the machine has.
    """
    found = shutil.which("kilix-rtsp")
    if found:
        return [found]
    local = os.path.join(os.path.expanduser("~"), ".local", "bin", "kilix-rtsp")
    if os.access(local, os.X_OK):
        return [local]
    built = os.path.join(
        sources.component_dir("kilix-modules/kilix-rtsp"), "build", "kilix-rtsp")
    if os.access(built, os.X_OK):
        return [built]
    return None


# ── the config file, read ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Camera:
    name: str
    main: bool
    sub: bool


@dataclass(frozen=True)
class Group:
    name: str
    members: tuple[str, ...]


def load(path: str) -> tuple[list[Camera], list[Group]]:
    """Names and tiers for the menu; URLs are never kept.

    A missing or unreadable file is a state, not a fault — a fresh machine
    has no cameras yet, and the empty list is what guides to `n`. The parse
    is deliberately tolerant: kilix-rtsp's own loader remains the authority
    on whether the file is valid, this one only needs enough to draw a menu.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return [], []
    cameras: list[Camera] = []
    groups: list[Group] = []
    tiers: dict[str, bool] = {}
    members: list[str] = []
    kind, current = "", ""
    for raw in lines:
        text = raw.strip()
        if not text or text.startswith(("#", ";")):
            continue
        if text.startswith("["):
            if kind == "camera" and current:
                cameras.append(Camera(current, tiers.get("main", False),
                                      tiers.get("sub", False)))
            elif kind == "group" and current:
                groups.append(Group(current, tuple(members)))
            kind, current, tiers, members = "", "", {}, []
            if '"' in text:
                head, _, rest = text[1:].partition('"')
                name, _, _ = rest.rpartition('"')
                if head.strip() in ("camera", "group") and name:
                    kind, current = head.strip(), name
            continue
        key, _, value = text.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if kind == "camera" and key in ("main", "sub"):
            tiers[key] = bool(value)
        elif kind == "group" and key == "cameras":
            members = [part.strip() for part in value.split(",")
                       if part.strip()]
    if kind == "camera" and current:
        cameras.append(Camera(current, tiers.get("main", False),
                              tiers.get("sub", False)))
    elif kind == "group" and current:
        groups.append(Group(current, tuple(members)))
    return cameras, groups


# ── the config file, written ───────────────────────────────────────────────────


def name_error(name: str, taken: set[str]) -> str | None:
    """Why this name cannot head a [camera "..."] section, or None."""
    if not name:
        return "a profile needs a name"
    if len(name) > NAME_MAX:
        return f"names are at most {NAME_MAX} characters"
    if any(mark in name for mark in '"[]'):
        return 'quotes and brackets break the [camera "name"] header'
    if name != name.strip():
        return "leading or trailing spaces make a name nobody can type"
    if name in taken:
        return f"'{name}' is already configured"
    return None


def url_error(url: str) -> str | None:
    """Why this URL cannot be a tier, or None. Empty is the caller's concern."""
    if not url:
        return None
    if not url.startswith("rtsp://"):
        return "stream URLs start with rtsp://"
    if len(url) > URL_MAX:
        return f"URLs are at most {URL_MAX} characters"
    if any(mark.isspace() for mark in url):
        return "a URL cannot contain spaces"
    return None


def add_camera(path: str, name: str, main: str, sub: str) -> None:
    """Append one profile to cameras.conf. Raises ValueError or OSError."""
    cameras, groups = load(path)
    taken = {camera.name for camera in cameras} | {group.name for group in groups}
    problem = name_error(name, taken)
    if problem:
        raise ValueError(problem)
    for url in (main, sub):
        problem = url_error(url)
        if problem:
            raise ValueError(problem)
    if not main and not sub:
        raise ValueError("a profile needs at least one of main and sub — "
                         "kilix-rtsp refuses a camera with neither")
    if len(cameras) >= CAMERAS_MAX:
        raise ValueError(f"kilix-rtsp reads at most {CAMERAS_MAX} cameras")
    stanza = f'[camera "{name}"]\n'
    if main:
        stanza += f"main = {main}\n"
    if sub:
        stanza += f"sub  = {sub}\n"
    stanza += "\n"
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    # O_CREAT with 0600: the file holds passwords from its first byte, and a
    # umask-shaped window where it does not is how secrets end up world-readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(stanza)
    # Repair an existing loose file while we are here: that chmod is the fix
    # kilix-rtsp's own error message names.
    os.chmod(path, 0o600)


# ── the tool ───────────────────────────────────────────────────────────────────


@dataclass
class Row:
    label: str
    hint: str = ""
    argv: tuple[str, ...] | None = None
    reason: str = ""


@dataclass
class State:
    cameras: list[Camera] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    selected: int = 0
    status: str = ""
    command: list[str] | None = None

    def __post_init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.cameras, self.groups = load(config_path())
        self.command = kilix_rtsp_command()

    def rows(self) -> list[Row]:
        out: list[Row] = []
        for camera in self.cameras:
            tiers = ("main+sub" if camera.main and camera.sub
                     else "main only" if camera.main else "sub only"
                     if camera.sub else "no streams")
            out.append(Row(camera.name, tiers, self._argv("view", camera.name),
                           "needs kilix-rtsp installed"))
        for group in self.groups:
            count = len(group.members)
            out.append(Row(f"mosaic: {group.name}",
                           f"{count} camera" + ("" if count == 1 else "s"),
                           self._argv("mosaic", group.name),
                           "needs kilix-rtsp installed"))
        if len(self.cameras) > 1:
            out.append(Row("mosaic: everything", f"{len(self.cameras)} cameras",
                           self._argv("mosaic"),
                           "needs kilix-rtsp installed"))
        out.append(Row("New camera profile", "writes cameras.conf", (),
                       ""))
        return out

    def _argv(self, verb: str, target: str = "") -> tuple[str, ...] | None:
        if self.command is None:
            return None
        return tuple(self.command + [verb] + ([target] if target else []))


def _view(state: State, row: Row) -> None:
    """Hand the terminal to kilix-rtsp and take it back on exit.

    A clean quit returns to the menu at once; a failure printed something to
    the terminal that the repaint would erase before it could be read, so a
    non-zero exit waits for Enter instead.
    """
    curses.endwin()
    try:
        code = subprocess.call(list(row.argv))
    except OSError:
        code = 126
    if code:
        try:
            input(f"\n{row.label} exited {code} — press Enter to return ")
        except (EOFError, KeyboardInterrupt):
            pass
    curses.flushinp()
    state.status = f"{row.label} exited {code}" if code else ""


def _prompt(question: str) -> str | None:
    """One line of text outside curses, None on cancellation."""
    suffix = " (Enter to skip)" if question.endswith("URL") else ""
    try:
        answer = input(f"{question}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return answer.strip()


def _new_profile(state: State) -> None:
    """Create one stream profile and save it to cameras.conf.

    Prompts run outside curses, the way kilix-rollout-resume's installer
    questions do: the answers are secrets-adjacent (a URL embeds credentials),
    so they belong on the plain terminal, not painted into a frame tests and
    screenshots capture.
    """
    curses.endwin()
    try:
        print(f"New camera profile — appended to {config_path()}")
        taken = {camera.name for camera in state.cameras}
        taken |= {group.name for group in state.groups}
        while True:
            name = _prompt("Camera name")
            if name is None:
                state.status = "cancelled"
                return
            problem = name_error(name, taken)
            if problem is None:
                break
            print(f"  {problem}")
        print("  A password containing / ? # must be percent-encoded "
              "(%2F %3F %23); @ and : need nothing.")
        urls: dict[str, str] = {}
        for tier in ("Main", "Sub"):
            while True:
                url = _prompt(f"{tier} stream URL")
                if url is None:
                    state.status = "cancelled"
                    return
                problem = url_error(url)
                if problem is None:
                    urls[tier.lower()] = url
                    break
                print(f"  {problem}")
        if not urls["main"] and not urls["sub"]:
            state.status = "a profile needs at least one URL — nothing saved"
            return
        try:
            add_camera(config_path(), name, urls["main"], urls["sub"])
        except (ValueError, OSError) as error:
            state.status = f"not saved: {error}"
            return
        state.reload()
        state.status = f"saved '{name}' — Enter views it"
    finally:
        curses.flushinp()


def render(surface, state: State) -> None:
    rows = state.rows()
    summary = state.status
    if not summary:
        count = len(state.cameras)
        summary = (f"{count} camera" + ("" if count == 1 else "s")
                   + f" configured · {config_path()}")
    body = shell.draw(
        surface,
        title="Cameras",
        sections=("Streams",),
        summary=summary,
        footer="↑↓ move · Enter view · n new profile · r reload · q quit",
        summary_role="accent" if state.status else "muted",
    )
    top = body.top
    if not state.cameras:
        shell.put(surface, body.top, body.left,
                  "No cameras configured yet.")
        shell.put(surface, body.top + 1, body.left,
                  "n writes the first stream profile; kilix-rtsp's "
                  "examples/cameras.conf.example explains the format.",
                  shell.tango.attr("muted"))
        top += 3
    state.selected = max(0, min(state.selected, len(rows) - 1))
    for index, row in enumerate(rows):
        if top + index >= body.bottom:
            break
        selected = index == state.selected
        marker = "▶" if selected else " "
        available = row.argv is not None
        hint = row.hint if available else row.reason
        body_text = f" {marker} {row.label}"
        pad = body.width - len(body_text) - len(hint) - 1
        attr = shell.tango.attr("selected") if selected else 0
        if not available and not selected:
            attr = shell.tango.attr("muted")
        shell.put(surface, top + index, body.left - 1,
                  f"{body_text}{' ' * max(1, pad)}{hint}"[: body.width + 1],
                  attr)


def handle(key: int, state: State) -> bool:
    if keymap.is_quit(key):
        return False
    if keymap.is_refresh(key):
        state.reload()
        state.status = ""
        return True
    if key in (ord("n"), ord("N")):
        _new_profile(state)
        return True
    if (step := keymap.direction(key)):
        state.selected = max(0, min(len(state.rows()) - 1,
                                    state.selected + step))
        return True
    if key in keymap.SELECT or key in keymap.RIGHT:
        rows = state.rows()
        if state.selected < len(rows):
            row = rows[state.selected]
            if not row.argv:
                state.status = row.reason
            elif row.argv == ():
                _new_profile(state)
            else:
                _view(state, row)
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as out:
            out.write(app.render_to_text(render, state) + "\n")
        return 0
    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
