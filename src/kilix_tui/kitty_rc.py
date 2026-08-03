"""An authenticated client for the terminal's own remote control.

Kilix runs kitty with `allow_remote_control socket`: requests over the local
instance socket (found via `KITTY_LISTEN_ON`) are accepted unconditionally,
while TTY-borne requests still need the private credential each pane receives
through `KILIX_RC_PASSWORD_FILE`. On the credentialed path the scope lives at
the terminal, not here: the password line kilix writes names exactly the
commands it will honour, so a tool asking for anything outside that set is
refused by the terminal even though it holds the credential. This module is
therefore a convenience, never a privilege — it cannot do more than the pane it
runs in was already allowed to do.

Everything degrades rather than raising on import, because these tools are also
expected to run over `ssh`, inside `tmux`, and from a bare checkout where there
is no terminal to talk to. `available()` is the check; `Unavailable` is what a
call raises when it is used anyway.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

TIMEOUT = 5.0


class Unavailable(RuntimeError):
    """No reachable terminal, or the terminal refused the command."""


# ── the shape a switcher actually wants ──────────────────────────────────────


@dataclass
class Pane:
    """One kitty window — a pane, in the vocabulary Kilix presents to users."""

    id: int
    title: str = ""
    process: str = ""
    argv: tuple[str, ...] = ()
    cwd: str = ""
    is_focused: bool = False
    page_id: int = 0
    page_title: str = ""
    os_window_id: int = 0

    @property
    def label(self) -> str:
        return self.process or self.title or "(untitled)"

    def matches(self, needle: str) -> bool:
        if not needle:
            return True
        needle = needle.casefold()
        return any(
            needle in field.casefold()
            for field in (
                self.title, self.process, self.cwd, self.page_title,
                " ".join(self.argv),
            )
        )


@dataclass
class Page:
    """One kitty tab — a page, in Kilix's vocabulary."""

    id: int
    title: str = ""
    index: int = 0
    is_active: bool = False
    os_window_id: int = 0
    panes: list[Pane] = field(default_factory=list)

    @property
    def cwd(self) -> str:
        for pane in self.panes:
            if pane.is_focused:
                return pane.cwd
        return self.panes[0].cwd if self.panes else ""

    def matches(self, needle: str) -> bool:
        if not needle:
            return True
        return needle.casefold() in self.title.casefold() or any(
            pane.matches(needle) for pane in self.panes
        )


@dataclass
class Tree:
    pages: list[Page] = field(default_factory=list)

    @property
    def panes(self) -> list[Pane]:
        return [pane for page in self.pages for pane in page.panes]

    def active_page(self) -> Page | None:
        for page in self.pages:
            if page.is_active:
                return page
        return self.pages[0] if self.pages else None

    def focused_pane(self) -> Pane | None:
        for pane in self.panes:
            if pane.is_focused:
                return pane
        return None

    def page_of(self, pane_id: int) -> Page | None:
        for page in self.pages:
            if any(pane.id == pane_id for pane in page.panes):
                return page
        return None

    def pane_with_argument(self, argument: str) -> Pane | None:
        """Find a pane whose foreground argv contains one exact argument.

        UUID-like arguments are compared without surrounding braces and
        case-insensitively. This is how a manager can find the tab hosting a
        particular VM without relying on a mutable tab title.
        """
        wanted = argument.strip("{}").casefold()
        for pane in self.panes:
            if any(
                value.strip("{}").casefold() == wanted
                for value in pane.argv
            ):
                return pane
        return None

    def home_page(self) -> Page | None:
        """The page the *caller* is running on.

        Not the same question as `active_page()`. A tool launched as an overlay
        takes the focus, and one run from a shell in a background page never had
        it — so "this page" has to be answered from the caller's own pane id
        rather than from whatever the terminal currently considers active.
        """
        own = self_pane_id()
        if own:
            page = self.page_of(own)
            if page is not None:
                return page
        return self.active_page()


def self_pane_id() -> int:
    """The pane this process is running in, or 0 if it cannot tell."""
    try:
        return int(os.environ.get("KITTY_WINDOW_ID", "") or 0)
    except ValueError:
        return 0


# ── talking to the terminal ──────────────────────────────────────────────────


def _kitten() -> str:
    if configured := os.environ.get("KILIX_KITTEN"):
        return configured
    # The Kilix launcher keeps its engine private rather than publishing
    # `kitten` on PATH. Tools opened from an ordinary pane inherit the storage
    # roots, so resolve the sibling launcher the same way Kilix itself does.
    build = os.environ.get("KILIX_BUILD_DIRECTORY", "")
    prebuilt = os.environ.get("KILIX_PREBUILT_HOME", "")
    home = os.environ.get("KILIX_HOME", "")
    candidates = (
        os.path.join(build, "current/src/kitty/launcher/kitten") if build else "",
        os.path.join(prebuilt, "bin/kitten") if prebuilt else "",
        os.path.join(home, "src/kitty/launcher/kitten") if home else "",
    )
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return "kitten"


def available() -> bool:
    """True when there is a terminal to talk to and a credential for it."""
    if not os.environ.get("KITTY_LISTEN_ON"):
        return False
    password_file = os.environ.get("KILIX_RC_PASSWORD_FILE", "")
    if not password_file or not os.path.isfile(password_file):
        return False
    return shutil.which(_kitten()) is not None


def _run(args: list[str]) -> str:
    if not available():
        raise Unavailable(
            "not running inside a Kilix terminal "
            "(needs KITTY_LISTEN_ON and KILIX_RC_PASSWORD_FILE)"
        )
    command = [
        _kitten(), "@",
        "--password-file", os.environ["KILIX_RC_PASSWORD_FILE"],
        *args,
    ]
    try:
        done = subprocess.run(
            command, check=False, timeout=TIMEOUT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise Unavailable(f"the terminal did not answer in {TIMEOUT:g}s") from exc
    except OSError as exc:
        raise Unavailable(str(exc)) from exc
    if done.returncode != 0:
        detail = done.stderr.strip() or f"kitten exited {done.returncode}"
        raise Unavailable(detail)
    return done.stdout


def _process_name(window: dict[str, Any]) -> str:
    for process in reversed(window.get("foreground_processes") or []):
        cmdline = process.get("cmdline") or []
        if cmdline:
            return os.path.basename(cmdline[0]) or cmdline[0]
    return ""


def _process_argv(window: dict[str, Any]) -> tuple[str, ...]:
    for process in reversed(window.get("foreground_processes") or []):
        cmdline = process.get("cmdline") or []
        if cmdline:
            return tuple(str(value) for value in cmdline)
    return ()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("path") or value.get("cwd") or "")
    return str(value)


def parse(state: list[dict[str, Any]]) -> Tree:
    """Turn kitty's `ls` payload into the page/pane tree.

    Split out from `tree()` so the whole model is testable against a recorded
    payload without a terminal — which is the only way most of this gets
    exercised, since the tests do not run inside Kilix.
    """
    tree = Tree()
    index = 0
    for os_index, os_window in enumerate(state, 1):
        os_id = int(os_window.get("id") or os_window.get("os_window_id") or os_index)
        os_focused = bool(os_window.get("is_focused"))
        for tab in os_window.get("tabs") or []:
            index += 1
            windows = tab.get("windows") or []
            page = Page(
                id=int(tab.get("id") or 0),
                title=_text(tab.get("title")),
                index=index,
                is_active=bool(tab.get("is_active")) and os_focused,
                os_window_id=os_id,
            )
            for window in windows:
                page.panes.append(Pane(
                    id=int(window.get("id") or 0),
                    title=_text(window.get("title")),
                    process=_process_name(window),
                    argv=_process_argv(window),
                    cwd=_text(window.get("cwd")),
                    is_focused=(
                        bool(window.get("is_focused") or window.get("is_active"))
                        and page.is_active
                    ),
                    page_id=page.id,
                    page_title=page.title,
                    os_window_id=os_id,
                ))
            if not page.title and page.panes:
                page.title = page.panes[0].label
            tree.pages.append(page)
    return tree


def tree() -> Tree:
    """The live page/pane tree."""
    raw = _run(["ls"])
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Unavailable(f"could not parse the terminal's reply: {exc}") from exc
    if not isinstance(state, list):
        raise Unavailable("the terminal returned an unexpected payload")
    return parse(state)


def pane_with_argument(argument: str) -> Pane | None:
    """Return the live pane whose foreground command owns `argument`."""
    return tree().pane_with_argument(argument)


def focus_page(page_id: int) -> None:
    _run(["focus-tab", "--match", f"id:{page_id}"])


def focus_pane(pane_id: int) -> None:
    _run(["focus-window", "--match", f"id:{pane_id}"])


def pane_text(pane_id: int, *, lines: int = 0) -> str:
    """The visible text of a pane, newest last.

    This is the one genuinely sensitive call here — it reads what is on another
    pane's screen — and it is inside the scoped credential precisely because
    `kilix watch` already needed it. Kept to `--extent screen` so it can never
    pull scrollback a user has forgotten is there.
    """
    out = _run(["get-text", "--match", f"id:{pane_id}", "--extent", "screen"])
    if lines > 0:
        rows = out.splitlines()
        while rows and not rows[-1].strip():
            rows.pop()
        return "\n".join(rows[-lines:])
    return out


# ── the acting half, which needs the wider scope ─────────────────────────────
#
# `close_*` and `rename_page` are outside the credential Kilix grants by
# default. They raise `Unavailable` with the terminal's own refusal when the
# scope has not been widened, which is the honest failure: the tool is not
# broken, it was denied.


def close_pane(pane_id: int) -> None:
    _run(["close-window", "--match", f"id:{pane_id}"])


def close_page(page_id: int) -> None:
    _run(["close-tab", "--match", f"id:{page_id}"])


def rename_page(page_id: int, title: str) -> None:
    _run(["set-tab-title", "--match", f"id:{page_id}", title])


def launch_tab(
    argv: list[str] | tuple[str, ...],
    *,
    title: str,
    cwd: str | None = None,
    keep_focus: bool = True,
) -> int:
    """Open `argv` in a new page; return its pane id, or 0 if unreported.

    Fixed argv only, never a shell string — the same discipline Kilix 95's
    Start menu and Kilix Cap's launchers follow. `keep_focus` leaves the
    caller focused, which is what a desktop launching a background surface
    wants; pass False to follow the launch. Raises `Unavailable` when the
    credential's scope does not include `launch` — that refusal is the
    terminal's decision and is reported, not worked around.
    """
    args = ["launch", "--type=tab", "--tab-title", title]
    if cwd:
        args.append(f"--cwd={cwd}")
    if keep_focus:
        args.append("--keep-focus")
    out = _run([*args, "--", *argv]).strip()
    return int(out) if out.isdigit() else 0
