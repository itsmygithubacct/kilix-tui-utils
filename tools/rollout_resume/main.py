"""kilix-rollout-resume — recover coding-agent sessions whose terminal is gone.

Claude Code, Codex, and Kimi Code each persist a conversation as it happens and
each can reload one by ID, so a session whose window closed is still on disk.
This lists all three together, hands the current Kilix tab over to the one you
pick, and — because a picker is no use without the agent that owns the
transcript — installs or updates the agents themselves.
"""
from __future__ import annotations

import curses
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, shell  # noqa: E402
from kilix_rollout import (  # noqa: E402
    claude, codex, config, installer, kimi, launch, liveness, manage, menu,
    model, pacing, providers,
)
from kilix_rollout.errors import (  # noqa: E402
    BackendError, ConflictError, NotFoundError, PacingError, ResumeError,
    UsageError,
)
from kilix_rollout.model import RANGES, Session  # noqa: E402

VIEWS = ("candidates", "cut-off", "idle", "live", "orphaned", "invalid", "all")
AGENTS = ("all", "claude", "codex", "kimi")
PANES = ("sessions", "agents")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> float:
    """Parse a compact window such as 90m, 24h, or 7d."""
    value = text.strip().casefold()
    unit = _UNITS.get(value[-1:], 0)
    number = value[:-1] if unit else value
    try:
        amount = float(number)
    except ValueError:
        raise UsageError(f"invalid duration '{text}'") from None
    if amount <= 0:
        raise UsageError("duration must be greater than zero")
    return amount * (unit or 1)


# ── selection ────────────────────────────────────────────────────────────────

def visible(sessions: list[Session], view: str, agent: str) -> list[Session]:
    chosen = sessions
    if agent != "all":
        chosen = [item for item in chosen if item.provider == agent]
    normalized = {
        "resumable": "idle",
        "interrupted": "cut-off",
    }.get(view, view)
    if normalized == "candidates":
        return [item for item in chosen if item.resumable]
    if normalized == "all":
        return chosen
    return [item for item in chosen if item.state == normalized]


def resolve(sessions: list[Session], selector: str) -> Session:
    """Find one session by ID/prefix, transcript filename, or full path."""
    needle = selector.strip().casefold()
    if not needle:
        raise UsageError("a session ID is required")
    expanded = os.path.abspath(os.path.expanduser(selector)).casefold()
    matches = [
        item for item in sessions
        if item.session_id.casefold().startswith(needle)
        or os.path.basename(item.path).casefold() == needle
        or os.path.abspath(item.path).casefold() == expanded
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NotFoundError(f"no session matches '{selector}'")
    listed = ", ".join(item.short_id for item in matches[:6])
    raise ConflictError(f"'{selector}' is ambiguous: {listed}")


# ── state ────────────────────────────────────────────────────────────────────

class State:
    def __init__(
        self,
        *,
        roots: dict[str, str] | None = None,
        include_archived: bool = False,
        include_orphans: bool = True,
        tb: str = "",
        no_log: bool = False,
        initial_since: float | None = None,
        executables: dict[str, str] | None = None,
        gap: float | None = None,
        provider_keys: tuple[str, ...] | None = None,
    ) -> None:
        self.pane = 0
        self.view = 0
        self.provider_keys = provider_keys
        self.agent = (
            AGENTS.index(provider_keys[0])
            if provider_keys and len(provider_keys) == 1 else 0)
        self.window = model.DEFAULT_RANGE
        self.custom_since = initial_since
        if initial_since is not None:
            known_range = next((
                index for index, (_label, seconds) in enumerate(RANGES)
                if seconds == initial_since
            ), None)
            if known_range is not None:
                self.window = known_range
                self.custom_since = None
        self.yolo = config.yolo_default()
        self.query = ""
        self.marked: set[str] = set()
        self.roots = roots or {}
        self.include_archived = include_archived
        self.include_orphans = include_orphans
        self.tb = tb
        self.no_log = no_log
        self.executables = executables or {}
        self.gap = config.launch_gap() if gap is None else gap
        self.selected = 0
        self.agent_row = 0
        self.offset = 0
        self.status = ""
        self.screen = None
        self.sessions: list[Session] = []
        self.agents: list[dict] = []
        self.refresh()
        # Nothing lost recently is the common case on a machine that has been
        # idle; widen once rather than showing an empty list.
        while (initial_since is None and self.custom_since is None
               and not self.sessions
               and self.window < len(RANGES) - 1):
            self.window += 1
            self.refresh()
            if self.sessions:
                self.status = (f"Nothing in the last {RANGES[self.window - 1][0]}; "
                               f"showing the last {RANGES[self.window][0]}.")
        if not any(row["installed"] for row in self.agents):
            self.pane = 1
            self.status = "No coding agent found — press Enter to install one."

    def refresh(self) -> None:
        self.sessions = providers.discover(
            self.provider_keys,
            since=(self.custom_since
                   if self.custom_since is not None
                   else RANGES[self.window][1]),
            roots=self.roots,
            include_archived=self.include_archived,
            include_orphans=self.include_orphans,
        )
        self.agents = manage.status(providers.PROVIDERS)
        known = {item.session_id for item in self.sessions}
        self.marked.intersection_update(known)
        self.selected = min(self.selected, max(0, len(self.rows) - 1))

    @property
    def rows(self) -> list[Session]:
        rows = visible(self.sessions, VIEWS[self.view], AGENTS[self.agent])
        needle = self.query.strip().casefold()
        if not needle:
            return rows
        return [
            item for item in rows
            if needle in "\n".join((
                item.provider,
                item.session_id,
                item.project,
                item.cwd,
                item.original_cwd,
                item.title,
                item.last_user_message,
                item.last_agent_message,
                item.path,
            )).casefold()
        ]

    @property
    def current(self) -> Session | None:
        rows = self.rows
        return rows[self.selected] if rows else None

    @property
    def current_agent(self):
        return providers.PROVIDERS[self.agent_row]

    @property
    def range_label(self) -> str:
        if self.custom_since is None:
            return RANGES[self.window][0]
        if self.custom_since <= 0:
            return "all"
        return f"{self.custom_since:g}s"


# ── rendering ────────────────────────────────────────────────────────────────

def _row_text(
    item: Session,
    width: int,
    *,
    marked: bool = False,
    selected: bool = False,
) -> str:
    prefix = (f"{'▶' if selected else ' '}{'*' if marked else ' '} "
              f"{item.state.upper():<8}"
              f"{item.provider:<7}{item.age():>4}  "
              f"{item.project[:16]:<16}  {item.short_id:<13}  ")
    return prefix + (item.title or "(no recorded prompt)")[:max(0, width - len(prefix))]


def render(surface, state: State) -> None:
    # Keep the real curses window available while a paced launch temporarily
    # owns the event loop. Headless TextSurface objects deliberately lack
    # getch(), so screenshots and tests never become interactive.
    if hasattr(surface, "getch"):
        state.screen = surface
    pane = PANES[state.pane]
    context = (
        f"view {VIEWS[state.view]} · agent {AGENTS[state.agent]} · "
        f"range {state.range_label}"
        f"{f' · filter {state.query}' if state.query else ''}"
        f"{f' · {len(state.marked)} marked' if state.marked else ''}"
        f"{' · YOLO' if state.yolo else ''}"
    )
    footer = (
        "Enter resume · x tmux · A attach · Space mark · R restore · "
        "/ filter · y yolo · q quit"
        if pane == "sessions" else
        "Enter install/update · Tab sessions · m sync menu · "
        "r refresh · ? help · q quit"
    )
    body = shell.draw(
        surface,
        title="Rollout Resume",
        sections=("Sessions", "Agents"),
        active=state.pane,
        summary=state.status or context,
        footer=footer,
        summary_role=(
            "alert" if state.yolo else "accent" if state.status else "muted"
        ),
    )

    if pane == "agents":
        _render_agents(surface, state, body)
    else:
        _render_sessions(surface, state, body)


def _render_sessions(surface, state: State, body: shell.Body) -> None:
    shell.put(
        surface, body.top, body.left,
        "   STATE   AGENT   AGE  PROJECT           SESSION        CONVERSATION",
        shell.tango.attr("title"),
    )
    rows = state.rows
    capacity = max(1, body.height - 2)
    if state.selected < state.offset:
        state.offset = state.selected
    if state.selected >= state.offset + capacity:
        state.offset = state.selected - capacity + 1
    state.offset = max(
        0, min(state.offset, max(0, len(rows) - capacity)))

    if not rows:
        shell.put(surface, body.top + 2, body.left + 1,
                  "(no sessions match this view)",
                  shell.tango.attr("muted"))
    for line, index in enumerate(
        range(state.offset, min(len(rows), state.offset + capacity)),
        start=body.top + 1,
    ):
        item = rows[index]
        selected = index == state.selected
        shell.put(
            surface, line, body.left,
            _row_text(
                item, body.width,
                marked=item.session_id in state.marked,
                selected=selected,
            ),
            shell.tango.attr("selected") if selected else 0,
        )

    chosen = state.current
    if chosen is not None and body.height:
        shell.put(
            surface, body.bottom - 1, body.left,
            chosen.cwd or "(no working directory)",
            shell.tango.attr("muted"),
        )


def _render_agents(surface, state: State, body: shell.Body) -> None:
    shell.put(surface, body.top, body.left,
              "  AGENT         COMMAND   STATUS",
              shell.tango.attr("title"))
    for line, row in enumerate(state.agents, start=body.top + 1):
        selected = line - body.top - 1 == state.agent_row
        mark = "installed" if row["installed"] else "NOT INSTALLED"
        text = (
            f"{'▶' if selected else ' '} "
            f"{row['label']:<14}{row['command']:<10}{mark}"
        )
        shell.put(
            surface, line, body.left, text,
            shell.tango.attr("selected") if selected else
            (shell.tango.attr("alert") if not row["installed"] else 0),
        )

    item = state.current_agent
    row = state.agents[state.agent_row]
    if row["installed"]:
        shell.put(surface, body.bottom - 2, body.left,
                  f"Update with: {' '.join(item.update_argv)}",
                  shell.tango.attr("muted"))
        shell.put(surface, body.bottom - 1, body.left,
                  f"Installed at: {row['path']}",
                  shell.tango.attr("muted"))
    else:
        shell.put(surface, body.bottom - 2, body.left,
                  f"Install with: {item.install_shell}",
                  shell.tango.attr("muted"))
        shell.put(surface, body.bottom - 1, body.left,
                  f"Documented at: {item.install_source}",
                  shell.tango.attr("muted"))


# ── interaction ──────────────────────────────────────────────────────────────

def _ask(question: str) -> bool:
    """Drop out of curses to ask a yes/no question, then return to it."""
    curses.endwin()
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    curses.flushinp()
    return answer.strip().casefold() in ("y", "yes")


def _prompt(question: str, default: str = "") -> str | None:
    """Drop out of curses for one line of text, preserving cancellation."""
    curses.endwin()
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        curses.flushinp()
        return None
    curses.flushinp()
    return answer if answer else default


def _confirm_cli(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().casefold() in ("y", "yes")


def _shell_out(run) -> str:
    """Run something that writes to the terminal, then return to curses."""
    curses.endwin()
    try:
        detail = run()
    finally:
        try:
            input("\nPress Enter to return to kilix-rollout-resume… ")
        except (EOFError, KeyboardInterrupt):
            pass
        curses.flushinp()
    return detail


def _resume_here(state: State) -> bool:
    chosen = state.current
    if chosen is None:
        state.status = "Nothing selected."
        return True
    if chosen.state == "live":
        state.status = (f"Protected: a running process still owns this session "
                        f"(PID {', '.join(str(pid) for pid in chosen.pids)}).")
        return True
    try:
        launch.check_installed(chosen)
        launch.working_directory(chosen)
    except RuntimeError as error:
        state.status = str(error)
        return True
    curses.endwin()
    launch.hand_over(
        chosen,
        yolo=state.yolo,
        executable=state.executables.get(chosen.provider, ""),
    )   # replaces this process
    return False


def _resume_tmux(state: State, *, offer_attach: bool = False) -> bool:
    chosen = state.current
    if chosen is None or chosen.state == "live":
        state.status = "Select a resumable session first."
        return True
    try:
        pacer = pacing.LaunchPacer(interval=state.gap)
        remaining = pacer.remaining()
        wait_note = f" after a {remaining:.0f}s wait" if remaining else ""
        unsafe_note = " with approval checks disabled" if state.yolo else ""
        if not _ask(
            f"Resume {chosen.short_id} in tmux{unsafe_note}{wait_note}?"
        ):
            state.status = "Resume cancelled."
            return True
        if remaining:
            state.status = (
                f"Waiting {remaining:.0f}s for the shared rate-limit guard…")
        name = launch.start_detached(
            chosen, yolo=state.yolo, tb=state.tb, no_log=state.no_log,
            executable=state.executables.get(chosen.provider, ""),
            pacer=pacer,
            on_wait=lambda remaining, interval: _paced_wait(
                state, remaining, interval, 1, 1, chosen.short_id))
    except KeyboardInterrupt:
        state.status = "Launch cancelled during the rate-limit wait."
        return True
    except RuntimeError as error:
        state.status = str(error)
        return True
    state.status = f"Started detached tmux session '{name}'. Attach with: tmux attach -t {name}"
    if offer_attach and _ask(f"Attach to '{name}' now?"):
        launch.attach(name, tb=state.tb)
    return True


def _toggle_mark(state: State) -> bool:
    chosen = state.current
    if chosen is None:
        state.status = "Nothing selected."
        return True
    if not chosen.resumable:
        state.status = f"{chosen.state} sessions cannot be restored."
        return True
    if chosen.session_id in state.marked:
        state.marked.remove(chosen.session_id)
    else:
        state.marked.add(chosen.session_id)
        state.selected = min(
            max(0, len(state.rows) - 1), state.selected + 1)
    state.status = f"{len(state.marked)} session(s) marked."
    return True


def _toggle_all(state: State) -> bool:
    candidates = {item.session_id for item in state.rows if item.resumable}
    if candidates and candidates <= state.marked:
        state.marked -= candidates
        state.status = "Cleared every visible mark."
    else:
        state.marked |= candidates
        state.status = f"{len(state.marked)} session(s) marked."
    return True


def _paced_wait(
    state: State,
    remaining: float,
    interval: float,
    position: int,
    total: int,
    name: str,
) -> bool | None:
    """Refresh the countdown and let q/Escape stop the pending launch."""
    state.status = (
        f"[{position}/{total}] waiting {remaining:.0f}s of {interval:.0f}s "
        f"before '{name}' — press q to stop.")
    screen = state.screen
    if screen is None:
        return None
    try:
        screen.erase()
        render(screen, state)
        screen.refresh()
        screen.nodelay(True)
        try:
            key = screen.getch()
        finally:
            screen.nodelay(False)
    except curses.error:
        return None
    if key in (ord("q"), 27):
        return False
    return None


def _restore_marked(state: State) -> bool:
    chosen = sorted(
        (item for item in state.sessions if item.session_id in state.marked),
        key=lambda item: item.updated,
    )
    if not chosen:
        state.status = "No sessions are marked; press Space to mark one."
        return True
    gap = state.gap
    pacer = pacing.LaunchPacer(interval=gap)
    span = pacer.remaining() + gap * max(0, len(chosen) - 1)
    if not _ask(
        f"Restore {len(chosen)} session(s) over about {span / 60:.1f} minutes?"
    ):
        state.status = "Restore cancelled."
        return True
    restored = 0
    failures = 0
    handled: set[str] = set()

    def record_result(result, position: int, total: int) -> None:
        nonlocal restored, failures
        item = result["session"]
        handled.add(item.session_id)
        if result["ok"]:
            restored += 1
            state.marked.discard(item.session_id)
            state.status = (
                f"[{position}/{total}] started '{result['detail']}'.")
        else:
            failures += 1
            state.status = f"{item.short_id}: {result['detail']}"
        screen = state.screen
        if screen is not None:
            try:
                screen.erase()
                render(screen, state)
                screen.refresh()
            except curses.error:
                pass

    positions = {
        item.session_id: index
        for index, item in enumerate(chosen, start=1)
    }
    try:
        results = launch.restore_all(
            chosen,
            gap=gap,
            yolo=state.yolo,
            tb=state.tb,
            no_log=state.no_log,
            executables=state.executables,
            pacer=pacer,
            on_wait=lambda remaining, item: _paced_wait(
                state, remaining, gap, positions[item.session_id],
                len(chosen), item.short_id),
            on_result=record_result,
        )
    except KeyboardInterrupt:
        state.refresh()
        state.status = (
            f"Stopped after {restored} of {len(chosen)}; "
            f"{len(chosen) - restored} still marked.")
        return True
    # Test doubles and third-party callers may not invoke on_result. Process
    # anything the returned list has not already reported.
    for position, result in enumerate(results, start=1):
        if result["session"].session_id not in handled:
            record_result(result, position, len(results))
    state.refresh()
    state.status = f"Restored {restored} session(s); {failures} failed."
    return True


def _toggle_yolo(state: State) -> bool:
    """Turn approval prompts off for launches from this picker.

    Turning it on is confirmed once rather than on every launch: the header
    keeps saying YOLO for as long as it is on, so the state stays visible
    without a prompt in front of every resume.
    """
    if state.yolo:
        state.yolo = False
        state.status = "YOLO off — resumed agents ask before acting."
        return True
    flags = ", ".join(sorted({config.yolo_flag(item.key)
                              for item in providers.PROVIDERS}))
    if _ask(f"Resume agents with approval prompts disabled ({flags})?"):
        state.yolo = True
        state.status = ("YOLO on — resumed agents will run commands without "
                        "asking. Press y again to turn it off.")
    else:
        state.status = "YOLO stays off."
    return True


def _install_or_update(state: State) -> bool:
    item = state.current_agent
    row = state.agents[state.agent_row]
    if row["installed"]:
        if not _ask(f"Update {item.label} with `{' '.join(item.update_argv)}`?"):
            state.status = "Update cancelled."
            return True
        state.status = _shell_out(lambda: _run_update(item))
    else:
        print()
        if not _ask(f"Install {item.label}?  This runs, from {item.install_source}:\n"
                    f"    {item.install_shell}\nProceed?"):
            state.status = "Install cancelled."
            return True
        state.status = _shell_out(lambda: _run_install(item))
    state.refresh()
    return True


def _run_install(item) -> str:
    print(f"$ {item.install_shell}\n")
    code = manage.run_install(item)
    if code != 0:
        return f"{item.label} install exited {code}."
    menu.sync(providers.PROVIDERS)
    return f"{item.label} installed; start-menu entries updated."


def _run_update(item) -> str:
    print(f"$ {' '.join(item.update_argv)}\n")
    code = manage.run_update(item)
    return (f"{item.label} is up to date." if code == 0
            else f"{item.label} update exited {code}.")


def _help(state: State) -> bool:
    state.status = (
        "Enter hands over · x starts detached · A offers attach · Space/* mark "
        "one/all · R restores marked · / filters · t/v/a change range/view/agent.")
    return True


def handle(key: int, state: State) -> bool:
    if keymap.is_quit(key):
        return False
    if key == ord("\t"):
        state.pane = (state.pane + 1) % len(PANES)
        return True
    # Uppercase R is the provider tools' paced bulk-restore binding.
    if keymap.is_refresh(key) and key != ord("R"):
        state.refresh()
        state.status = f"{len(state.sessions)} session(s) found."
        return True
    if keymap.is_help(key) or key in (ord("?"), ord("h")):
        return _help(state)

    if PANES[state.pane] == "agents":
        step = keymap.direction(key)
        if step:
            state.agent_row = max(0, min(len(state.agents) - 1, state.agent_row + step))
            return True
        if key in keymap.SELECT:
            return _install_or_update(state)
        if key == ord("m"):
            result = menu.sync(providers.PROVIDERS)
            state.status = (f"Start menu: {len(result['written'])} entr(ies) written, "
                            f"{len(result['removed'])} removed.")
            return True
        return True

    step = keymap.direction(key)
    if step:
        state.selected = max(0, min(len(state.rows) - 1, state.selected + step))
        return True
    if key in (ord("\n"), ord("\r"), curses.KEY_ENTER):
        return _resume_here(state)
    if key == ord("x"):
        return _resume_tmux(state)
    if key == ord("A"):
        return _resume_tmux(state, offer_attach=True)
    if key == ord(" "):
        return _toggle_mark(state)
    if key == ord("*"):
        return _toggle_all(state)
    if key == ord("R"):
        return _restore_marked(state)
    if key == ord("/"):
        value = _prompt("Filter", state.query)
        if value is not None:
            state.query = value
            state.selected = 0
            state.offset = 0
            state.status = (
                f"Filter: {state.query or '(none)'} — {len(state.rows)} shown.")
        return True
    if key == ord("y"):
        return _toggle_yolo(state)
    if key == ord("!"):
        return _toggle_yolo(state)
    if key == ord("t"):
        if state.custom_since is not None:
            state.custom_since = None
            state.window = model.DEFAULT_RANGE
        else:
            state.window = (state.window + 1) % len(RANGES)
        state.selected = 0
        state.refresh()
        state.status = (f"Showing the last {state.range_label}: "
                        f"{len(state.sessions)} session(s).")
        return True
    if key == ord("v"):
        state.view = (state.view + 1) % len(VIEWS)
        state.selected = 0
        return True
    if key == ord("a"):
        state.agent = (state.agent + 1) % len(AGENTS)
        state.selected = 0
        return True
    if key == curses.KEY_HOME:
        state.selected = 0
        return True
    if key == curses.KEY_END:
        state.selected = max(0, len(state.rows) - 1)
        return True
    if key == curses.KEY_NPAGE:
        state.selected = min(max(0, len(state.rows) - 1), state.selected + 10)
        return True
    if key == curses.KEY_PPAGE:
        state.selected = max(0, state.selected - 10)
        return True
    return True


# ── command line ─────────────────────────────────────────────────────────────

def _session_dict(item: Session) -> dict[str, object]:
    try:
        size = os.path.getsize(item.path)
    except OSError:
        size = 0
    record = item.to_dict()
    record["updated_at"] = datetime.fromtimestamp(
        item.updated, timezone.utc).isoformat()
    record["started_at"] = (
        datetime.fromtimestamp(item.started, timezone.utc).isoformat()
        if item.started is not None else None)
    record["size_bytes"] = size
    return record


def _print_sessions(
    sessions: list[Session],
    as_json: bool,
    *,
    no_header: bool = False,
) -> None:
    if as_json:
        print(json.dumps([_session_dict(item) for item in sessions], indent=2))
        return
    if not sessions:
        print("(no saved sessions)")
        return
    if not no_header:
        print("STATE   AGENT   AGE  PROJECT           SESSION        CONVERSATION")
    for item in sessions:
        print(f"{item.state.upper():<8}{item.provider:<7}{item.age():>4}  "
              f"{item.project[:16]:<16}  {item.short_id:<13}  "
              f"{item.title or '(no recorded prompt)'}")


def _filter_sessions(
    sessions: list[Session],
    *,
    state: str = "all",
    query: str = "",
) -> list[Session]:
    aliases = {"resumable": "idle", "interrupted": "cut-off"}
    normalized = aliases.get(state, state)
    if normalized not in VIEWS:
        raise RuntimeError(
            f"unknown state '{state}'; use "
            + ", ".join((*VIEWS, *aliases)))
    chosen = visible(sessions, normalized, "all")
    needle = query.strip().casefold()
    if not needle:
        return chosen
    return [
        item for item in chosen
        if needle in "\n".join((
            item.provider, item.session_id, item.project, item.cwd,
            item.original_cwd, item.title, item.last_user_message,
            item.last_agent_message, item.path)).casefold()
    ]


def _cmd_show(item: Session, as_json: bool) -> int:
    record = _session_dict(item)
    if as_json:
        print(json.dumps(record, indent=2))
        return 0
    fields = (
        ("Agent", record["provider"]),
        ("Session", record["session_id"]),
        ("State", record["state"]),
        ("Legacy state", record["legacy_state"]),
        ("Resumable", "yes" if record["resumable"] else "no"),
        ("Archived", "yes" if record["archived"] else "no"),
        ("Started", record["started_at"] or "-"),
        ("Updated", record["updated_at"]),
        ("Project", record["project"]),
        ("Working directory", record["cwd"] or "-"),
        ("Original directory", record["original_cwd"] or "-"),
        ("Directory exists", "yes" if record["cwd_exists"] else "no"),
        ("Transcript", record["path"]),
        ("Size", f"{record['size_bytes']} bytes"),
        ("Live PIDs", ", ".join(map(str, record["live_pids"])) or "-"),
        ("Live status", record["live_status"] or "-"),
        ("Git branch", record["git_branch"] or "-"),
        ("Agent version", record["version"] or "-"),
        ("Entrypoint/source", record["entrypoint"] or "-"),
        ("Last turn event", record["last_turn_event"] or "-"),
        ("Pending tool", record["pending_tool"] or "-"),
        ("Last user message", record["last_user_message"] or "-"),
        ("Last agent message", record["last_agent_message"] or "-"),
        ("Conversation", record["title"] or "-"),
        ("Invalid reason", record["invalid_reason"] or "-"),
    )
    width = max(len(label) for label, _ in fields)
    for label, value in fields:
        print(f"{label.ljust(width)}  {value}")
    return 0


def _cmd_status(as_json: bool) -> int:
    rows = manage.status(providers.PROVIDERS)
    for row in rows:
        item = providers.provider(str(row["key"]))
        row["version"] = manage.version(item) if row["installed"] else ""
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        mark = "installed" if row["installed"] else "MISSING"
        print(f"{row['label']:<14}{mark:<12}{row['version'] or row['path'] or ''}")
    return 0 if any(row["installed"] for row in rows) else 1


def _cmd_install(key: str, assume_yes: bool) -> int:
    item = providers.provider(key)
    if manage.installed(item):
        print(f"{item.label} is already installed at {manage.installed(item)}")
        return 0
    plan = manage.install_plan(item)
    print(f"{item.label} install, as documented at {plan['source']}:\n\n"
          f"    {plan['command']}\n")
    if not assume_yes:
        try:
            answer = input("Run it? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().casefold() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    code = manage.run_install(item)
    if code == 0:
        menu.sync(providers.PROVIDERS)
    return code


def _cmd_update(key: str) -> int:
    item = providers.provider(key)
    if not manage.installed(item):
        print(f"{item.label} is not installed; run: kilix-rollout-resume install {key}",
              file=sys.stderr)
        return 1
    print(f"$ {' '.join(item.update_argv)}")
    return manage.run_update(item)


def _cmd_restore(
    sessions: list[Session],
    limit: int,
    gap: float,
    yolo: bool = False,
    *,
    dry_run: bool = False,
    as_json: bool = False,
    tb: str = "",
    no_log: bool = False,
    executables: dict[str, str] | None = None,
    force_live: bool = False,
    fork: bool = False,
    permission_mode: str = "",
    agent_model: str = "",
    prompt: str = "",
    assume_yes: bool = False,
    strict_targets: bool = False,
) -> int:
    eligible = lambda item: (  # noqa: E731
        item.resumable or (force_live and item.state == "live"))
    if strict_targets:
        rejected = [item for item in sessions if not eligible(item)]
        if rejected:
            item = rejected[0]
            if item.state == "orphaned":
                raise NotFoundError(
                    f"session {item.short_id} has no transcript left on disk")
            if item.state == "live":
                raise ConflictError(
                    f"session {item.short_id} is still owned by a live process")
            raise UsageError(
                f"session {item.short_id} is {item.state} and cannot be restored")
    eligible_sessions = [item for item in sessions if eligible(item)]
    chosen = eligible_sessions if strict_targets else eligible_sessions[:limit]
    if not chosen:
        print("[]" if as_json else "(nothing to restore)")
        return 0
    if not strict_targets:
        # Filtered discovery is newest-first; batches follow the timeline.
        # Explicit selectors retain the order the operator supplied.
        chosen.reverse()
    span = gap * max(0, len(chosen) - 1)
    pacer = pacing.LaunchPacer(interval=gap)
    executables = executables or {}
    if dry_run:
        taken = launch.tmux_sessions(tb=tb)
        plans = []
        offset = pacer.remaining()
        total_span = offset + span
        for index, item in enumerate(chosen):
            plan = launch.resume_plan(
                item, detached=True, yolo=yolo, taken=taken,
                executable=executables.get(item.provider, ""),
                force_live=force_live,
                fork=fork if item.provider == "claude" else False,
                permission_mode=(
                    permission_mode if item.provider == "claude" else ""),
                model=agent_model if item.provider == "claude" else "",
                prompt=prompt if item.provider == "claude" else "",
                tb=tb, no_log=no_log)
            taken.add(str(plan["tmux_name"]))
            plans.append({
                "order": index + 1,
                "wait_before": gap if index else round(offset, 3),
                "starts_in_seconds": round(offset + index * gap, 3),
                "state": item.legacy_state,
                "project": item.project,
                **plan,
            })
        if as_json:
            print(json.dumps({"dry_run": True, "gap": gap, "plans": plans}, indent=2))
        else:
            print(f"Dry run: {len(plans)} session(s), {gap:.0f}s apart "
                  f"(about {total_span / 60:.0f}m total).")
            for plan in plans:
                print(f"  {plan['order']:>2}. +{plan['starts_in_seconds']:>6.0f}s "
                      f"{plan['provider']:<7}"
                      f"{str(plan['session_id'])[:13]} -> "
                      f"{plan['tmux_name']}  {plan['command_text']}")
        return 0
    if not assume_yes and not _confirm_cli(
        f"Restore {len(chosen)} session(s), spaced {gap:g} seconds apart?"
    ):
        raise UsageError("restore cancelled; pass --yes to confirm unattended")
    if not as_json:
        print(f"Restoring {len(chosen)} session(s), {gap:.0f}s apart "
              f"(about {span / 60:.0f}m total).")
        if yolo:
            print("  approval prompts disabled for every agent in this batch.")
    announced: set[str] = set()

    def report_wait(remaining: float, item: Session) -> None:
        if item.session_id in announced:
            return
        announced.add(item.session_id)
        print(f"  waiting {remaining:.0f}s before {item.provider} "
              f"{item.short_id} (shared rate-limit guard)", file=sys.stderr)

    results = launch.restore_all(
        chosen, gap=gap, yolo=yolo, tb=tb, no_log=no_log,
        executables=executables, force_live=force_live, fork=fork,
        permission_mode=permission_mode, model=agent_model, prompt=prompt,
        pacer=pacer, on_wait=report_wait)
    if as_json:
        print(json.dumps([{
            "session": _session_dict(result["session"]),
            "ok": result["ok"],
            "detail": result["detail"],
        } for result in results], indent=2))
    else:
        for result in results:
            item = result["session"]
            mark = "started" if result["ok"] else "failed "
            print(f"  {mark} {item.provider:<7}{item.short_id}  {result['detail']}")
    return 0 if any(result["ok"] for result in results) else 1


def _take_flag(arguments: list[str], *names: str) -> bool:
    found = False
    for name in names:
        while name in arguments:
            arguments.remove(name)
            found = True
    return found


def _take_value(
    arguments: list[str],
    *names: str,
    default=None,
    convert=str,
):
    matches = [(arguments.index(name), name) for name in names if name in arguments]
    if not matches:
        return default
    index, name = min(matches)
    if index + 1 >= len(arguments):
        raise RuntimeError(f"{name} requires a value")
    raw = arguments[index + 1]
    del arguments[index:index + 2]
    try:
        return convert(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f"invalid value for {name}: {raw}") from None


def _configured_program(value: str, label: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.is_file() and (
            os.access(candidate, os.X_OK)
            or (label == "tb" and candidate.suffix == ".py")):
        return str(candidate.absolute())
    found = shutil.which(value)
    if found:
        return str(Path(found).absolute())
    raise RuntimeError(f"could not find {label}: {value}")


def _cmd_configure(arguments: list[str], as_json: bool) -> int:
    updates: dict[str, object] = {}
    for key in ("tmux", "tb", "claude", "codex", "kimi"):
        value = _take_value(arguments, f"--{key}", default="")
        clear = _take_flag(arguments, f"--clear-{key}")
        if value and clear:
            raise RuntimeError(
                f"--{key} and --clear-{key} cannot be used together")
        if value:
            updates[key] = _configured_program(value, key)
        elif clear:
            updates[key] = None
    gap = _take_value(arguments, "--gap", "--interval", default=None, convert=float)
    clear_gap = _take_flag(arguments, "--clear-gap", "--clear-interval")
    if gap is not None and clear_gap:
        raise RuntimeError("--gap and --clear-gap cannot be used together")
    if gap is not None:
        if gap < config.DEFAULT_GAP:
            raise RuntimeError(
                f"--gap must be at least {config.DEFAULT_GAP:.0f} seconds")
        updates["gap"] = gap
    elif clear_gap:
        updates["gap"] = None
    if arguments:
        raise RuntimeError("unexpected configure argument(s): " + " ".join(arguments))

    destination = config.config_path()
    if updates:
        config.write_config(updates)
    settings = config.load_config()
    payload = {"path": str(destination), "settings": settings}
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Configuration: {destination}")
        if not settings:
            print("(no overrides; PATH and the shared Kilix settings are used)")
        for key in sorted(settings):
            print(f"{key.ljust(7)}  {settings[key]}")
    return 0


def _doctor_rows(
    *,
    roots: dict[str, str] | None = None,
    include_archived: bool = False,
) -> list[dict[str, object]]:
    roots = roots or {}
    applications = Path(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
    ) / "applications"
    binary = Path(
        os.environ.get("XDG_BIN_HOME", str(Path.home() / ".local/bin"))
    ) / "kilix-rollout-resume"
    claude_projects = Path(
        roots.get("claude_projects") or Path(claude.home()) / "projects")
    codex_sessions = Path(
        roots.get("codex_sessions") or Path(codex.home()) / "sessions")
    transcript_roots = [
        ("Claude transcripts", claude_projects),
        ("Claude live-session registry", Path(claude.home()) / "sessions"),
        ("Codex transcripts", codex_sessions),
        ("Kimi transcripts", Path(kimi.home()) / "sessions"),
    ]
    if include_archived:
        transcript_roots.append((
            "Codex archived transcripts",
            codex_sessions.parent / "archived_sessions",
        ))
    rows: list[dict[str, object]] = [
        {"check": label, "value": str(path), "ok": path.exists(),
         "required": False}
        for label, path in transcript_roots
    ]
    for item in providers.PROVIDERS:
        path = config.resolve_program(item.key, item.command)
        rows.append({
            "check": item.label,
            "value": path or config.configured_program(item.key, item.command),
            "ok": bool(path),
            "required": False,
            "kind": "agent",
        })
    tmux = config.resolve_program("tmux", "tmux")
    configured_tb = config.configured_program("tb", "")
    tb = config.resolve_program("tb", "") if configured_tb else ""
    gap = config.launch_gap()
    rows.extend((
        {"check": "tmux", "value": tmux or config.configured_program("tmux", "tmux"),
         "ok": bool(tmux), "required": True},
        {"check": "tmux-cli logging backend",
         "value": tb or configured_tb or "native tmux selected",
         "ok": bool(tb) if configured_tb else True,
         "required": False},
        {"check": "launch interval", "value": f"{gap:g} seconds",
         "ok": gap >= pacing.MINIMUM_INTERVAL, "required": True},
        {"check": "installed command", "value": str(binary),
         "ok": binary.exists() or binary.is_symlink(), "required": False},
        {"check": "Kilix-95 entry",
         "value": str(applications / "kilix-rollout-resume.desktop"),
         "ok": (applications / "kilix-rollout-resume.desktop").is_file(),
         "required": False},
        {"check": "user configuration", "value": str(config.config_path()),
         "ok": config.config_path().is_file(), "required": False},
    ))
    return rows


def _cmd_doctor(
    as_json: bool,
    *,
    roots: dict[str, str] | None = None,
    include_archived: bool = False,
) -> int:
    rows = _doctor_rows(
        roots=roots, include_archived=include_archived)
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        width = max(len(str(row["check"])) for row in rows)
        for row in rows:
            marker = "ok" if row["ok"] else "MISSING"
            print(f"{str(row['check']).ljust(width)}  {marker:7}  {row['value']}")
    required_ok = all(row["ok"] for row in rows if row.get("required"))
    any_agent = any(row["ok"] for row in rows if row.get("kind") == "agent")
    return 0 if required_ok and any_agent else 1


def _cmd_prune(as_json: bool) -> int:
    directory = os.path.join(claude.home(), "sessions")
    removed = liveness.prune_registry(directory)
    if as_json:
        print(json.dumps({"directory": directory, "removed": removed}, indent=2))
    elif not removed:
        print(f"No stale Claude registry entries in {directory}.")
    else:
        for item in removed:
            print(f"removed PID {item['pid']}: {item['path']}")
    return 0


def _print_help() -> None:
    print(__doc__.strip())
    print(
        "\nCommands:\n"
        "  list|ls                 list saved sessions\n"
        "  show <id>               show one session in detail\n"
        "  resume <id>             hand this terminal to one agent\n"
        "  restore [id ...]        restore several sessions in detached tmux\n"
        "  doctor                  check transcripts, agents, tmux, and launchers\n"
        "  configure               show or set agent/tmux paths and launch gap\n"
        "  prune                   remove stale Claude live-session descriptors\n"
        "  install-launcher        install only this command and its menu entries\n"
        "  uninstall-launcher      remove its managed launcher files, not config\n"
        "  install|update <agent>  manage an agent; status reports all agents\n"
        "  sync-menu               refresh Kilix-95 entries\n"
        "  claude|codex <command>  provider namespace; --json uses legacy envelope\n"
        "\nSelection: --agent <name>, --since <90m|24h|7d>, --all-time, "
        "--state <state>, --query <text>, --limit <n>\n"
        "Sources:   --projects-dir <path>, --sessions-dir <path>, "
        "--archived, --no-orphans\n"
        "Resume:    --detached, --attach, --name <name>, --cwd <path>, "
        "--dry-run, --force-live\n"
        "Logging:   --tb <path> uses tmux-cli pane logging; --no-log disables it; "
        "--native-tmux bypasses tb\n"
        "Claude:    --fork, --permission-mode <mode>, --model <name>, "
        "--prompt <text>\n"
        "Safety:    --yolo, --no-yolo; JSON: --json, --envelope\n"
        "Agents:    " + ", ".join(item.key for item in providers.PROVIDERS)
    )


def _backend_options(arguments: list[str]) -> tuple[str, bool]:
    explicit = _take_value(arguments, "--tb", default="")
    native = _take_flag(arguments, "--native-tmux", "--no-tb")
    no_log = _take_flag(arguments, "--no-log")
    if explicit and native:
        raise UsageError("--tb and --native-tmux cannot be used together")
    if native:
        return "", no_log
    configured = explicit or config.configured_program("tb", "")
    if not configured:
        return "", no_log
    return _configured_program(configured, "tb"), no_log


def _cmd_install_launcher(arguments: list[str], as_json: bool) -> int:
    bin_dir = _take_value(arguments, "--bin-dir", default="")
    applications_dir = _take_value(
        arguments, "--applications-dir", default="")
    force = _take_flag(arguments, "--force")
    updates: dict[str, object] = {}
    for key in ("tb", "claude", "codex", "kimi"):
        value = _take_value(arguments, f"--{key}", default="")
        if value:
            updates[key] = _configured_program(value, key)
    gap = _take_value(
        arguments, "--gap", "--interval", default=None, convert=float)
    if gap is not None:
        if gap < pacing.MINIMUM_INTERVAL:
            raise UsageError(
                f"--interval must be at least "
                f"{pacing.MINIMUM_INTERVAL:.0f} seconds")
        updates["gap"] = gap
    if arguments:
        raise UsageError(
            "unexpected install-launcher argument(s): " + " ".join(arguments))
    if updates:
        config.write_config(updates)
    result = installer.install_launcher(
        Path(__file__),
        bin_dir=Path(bin_dir) if bin_dir else None,
        applications_dir=Path(applications_dir) if applications_dir else None,
        force=force,
    )
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"installed {result['command']}")
        print(f"wrote     {result['desktop_entry']}")
        print(f"config    {result['configuration']}")
    return 0


def _cmd_uninstall_launcher(arguments: list[str], as_json: bool) -> int:
    bin_dir = _take_value(arguments, "--bin-dir", default="")
    applications_dir = _take_value(
        arguments, "--applications-dir", default="")
    if arguments:
        raise UsageError(
            "unexpected uninstall-launcher argument(s): " + " ".join(arguments))
    result = installer.uninstall_launcher(
        bin_dir=Path(bin_dir) if bin_dir else None,
        applications_dir=Path(applications_dir) if applications_dir else None,
    )
    if as_json:
        print(json.dumps(result, indent=2))
    elif result["removed"]:
        for path in result["removed"]:
            print(f"removed {path}")
        print(f"preserved configuration {result['configuration']}")
    else:
        print("No managed rollout-resume launcher files were present.")
    return 0


def main(argv: list[str]) -> int:
    arguments = list(argv)
    screenshot = _take_value(arguments, "--screenshot", default="")
    as_json = _take_flag(arguments, "--json")
    no_header = _take_flag(arguments, "--no-header")
    assume_yes = _take_flag(arguments, "--yes", "-y")
    yolo = config.yolo_default()
    if _take_flag(arguments, "--yolo", "--dangerously-skip-permissions"):
        yolo = True
    if _take_flag(arguments, "--no-yolo"):
        yolo = False

    agent = _take_value(arguments, "--agent", default="")
    if agent and agent not in AGENTS:
        raise UsageError(
            f"unknown agent '{agent}'; use " + ", ".join(AGENTS))
    agent_filter = [agent] if agent and agent != "all" else None
    projects_dir = _take_value(arguments, "--projects-dir", default="")
    sessions_dir = _take_value(arguments, "--sessions-dir", default="")
    include_archived = _take_flag(arguments, "--archived")
    include_orphans = not _take_flag(arguments, "--no-orphans")
    roots: dict[str, str] = {}
    if projects_dir:
        roots["claude_projects"] = str(Path(projects_dir).expanduser())
    if sessions_dir:
        roots["codex_sessions"] = str(Path(sessions_dir).expanduser())
    since = RANGES[model.DEFAULT_RANGE][1]
    all_time = _take_flag(arguments, "--all-time")
    if all_time:
        since = 0.0
    since_text = _take_value(arguments, "--since", default="")
    if since_text:
        since = parse_duration(since_text)

    command = arguments.pop(0) if arguments else ""
    if command in ("-h", "--help", "help"):
        _print_help()
        return 0
    if command in ("--version", "version"):
        version_file = Path(__file__).resolve().parents[2] / "VERSION"
        version = version_file.read_text(encoding="utf-8").strip()
        print(f"kilix-rollout-resume {version}")
        return 0
    if command == "status":
        if arguments:
            raise RuntimeError("status takes no positional arguments")
        return _cmd_status(as_json)
    if command in ("install", "update"):
        if len(arguments) != 1:
            raise RuntimeError(
                f"usage: kilix-rollout-resume {command} <agent>")
        return (_cmd_install(arguments[0], assume_yes) if command == "install"
                else _cmd_update(arguments[0]))
    if command == "install-launcher":
        return _cmd_install_launcher(arguments, as_json)
    if command == "uninstall-launcher":
        return _cmd_uninstall_launcher(arguments, as_json)
    if command == "sync-menu":
        if arguments:
            raise RuntimeError("sync-menu takes no positional arguments")
        result = menu.sync(providers.PROVIDERS)
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            for path in result["written"]:
                print(f"wrote   {path}")
            for path in result["removed"]:
                print(f"removed {path}")
        return 0
    if command == "configure":
        return _cmd_configure(arguments, as_json)
    if command == "doctor":
        if arguments:
            raise RuntimeError("doctor takes no positional arguments")
        return _cmd_doctor(
            as_json, roots=roots, include_archived=include_archived)
    if command == "prune":
        if arguments:
            raise RuntimeError("prune takes no positional arguments")
        return _cmd_prune(as_json)

    def discover_sessions(
        *,
        window: float = since,
        selectors: tuple[str, ...] = (),
    ) -> list[Session]:
        return providers.discover(
            agent_filter,
            since=window,
            roots=roots,
            include_archived=include_archived,
            include_orphans=include_orphans,
            selectors=selectors,
        )

    if command in ("list", "ls"):
        state = _take_value(arguments, "--state", default="all")
        query = _take_value(arguments, "--query", "-q", default="")
        limit = _take_value(arguments, "--limit", default=100, convert=int)
        if limit < 1:
            raise RuntimeError("--limit must be greater than zero")
        if arguments:
            raise RuntimeError("unexpected list argument(s): " + " ".join(arguments))
        sessions = discover_sessions()
        _print_sessions(
            _filter_sessions(sessions, state=state, query=query)[:limit],
            as_json,
            no_header=no_header,
        )
        return 0
    if command == "show":
        if len(arguments) != 1:
            raise RuntimeError("usage: kilix-rollout-resume show <session-id>")
        sessions = discover_sessions(
            window=(since if (all_time or since_text) else 0.0),
            selectors=(arguments[0],),
        )
        return _cmd_show(resolve(sessions, arguments[0]), as_json)
    if command == "resume":
        tb, no_log = _backend_options(arguments)
        detached = _take_flag(arguments, "--detached", "--tmux-session")
        attach = _take_flag(arguments, "--attach")
        dry_run = _take_flag(arguments, "--dry-run")
        force_live = _take_flag(arguments, "--force-live")
        fork = _take_flag(arguments, "--fork")
        name = _take_value(arguments, "--name", default="")
        cwd = _take_value(arguments, "--cwd", default="")
        permission_mode = _take_value(
            arguments, "--permission-mode", default="")
        agent_model = _take_value(arguments, "--model", default="")
        prompt = _take_value(arguments, "--prompt", default="")
        gap = _take_value(
            arguments, "--gap", "--interval", default=None, convert=float)
        if gap is None:
            gap = config.launch_gap()
        if gap < pacing.MINIMUM_INTERVAL:
            raise RuntimeError(
                f"--gap must be at least {pacing.MINIMUM_INTERVAL:.0f} seconds")
        executable_options = {
            key: _take_value(arguments, f"--{key}", default="")
            for key in ("claude", "codex", "kimi")
        }
        if len(arguments) != 1:
            raise RuntimeError("usage: kilix-rollout-resume resume <session-id>")
        sessions = discover_sessions(
            window=(since if (all_time or since_text) else 0.0),
            selectors=(arguments[0],),
        )
        chosen = resolve(sessions, arguments[0])
        wrong = [key for key, value in executable_options.items()
                 if value and key != chosen.provider]
        if wrong:
            raise RuntimeError(
                f"--{wrong[0]} does not apply to a {chosen.provider} session")
        executable = executable_options[chosen.provider]
        if executable:
            executable = _configured_program(executable, chosen.provider)
        detached = detached or attach or bool(name) or bool(tb)
        if attach and as_json:
            raise RuntimeError("--attach and --json cannot be used together")
        if as_json and not (detached or dry_run):
            raise RuntimeError(
                "--json with a direct handover requires --dry-run or --detached")
        plan = launch.resume_plan(
            chosen, detached=detached, name=name, cwd=cwd, yolo=yolo,
            executable=executable, force_live=force_live, fork=fork,
            permission_mode=permission_mode, model=agent_model, prompt=prompt,
            tb=tb, no_log=no_log)
        if dry_run:
            wait = (pacing.LaunchPacer(interval=gap).remaining()
                    if detached else 0.0)
            payload = {
                "dry_run": True,
                "wait_seconds": round(wait, 3),
                "gap": gap,
                **plan,
            }
            if as_json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"Session : {plan['session_id']}")
                print(f"mode    : {'detached tmux' if detached else 'current terminal'}")
                if detached:
                    print(f"tmux    : {plan['tmux_name']}")
                    print(f"backend : {plan['tmux_backend']}")
                    print(f"logging : {'on' if plan['pane_logging'] else 'off'}")
                print(f"cwd     : {plan['cwd']}")
                print(f"command : {plan['command_text']}")
            return 0
        if detached:
            pacer = pacing.LaunchPacer(interval=gap)
            announced = False

            def report_wait(remaining: float, _interval: float) -> None:
                nonlocal announced
                if not announced:
                    print(f"kilix-rollout-resume: waiting {remaining:.0f}s "
                          "for the shared rate-limit guard", file=sys.stderr)
                    announced = True

            created = launch.start_detached(
                chosen, name=name, cwd=cwd, yolo=yolo, executable=executable,
                force_live=force_live, fork=fork,
                permission_mode=permission_mode, model=agent_model,
                prompt=prompt, tb=tb, no_log=no_log,
                pacer=pacer, on_wait=report_wait)
            if as_json:
                print(json.dumps({"created": True, **plan}, indent=2))
            else:
                print(f"Resumed {chosen.short_id} as tmux session "
                      f"'{created}' in {plan['cwd']}")
            if attach:
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise RuntimeError("attaching requires an interactive terminal")
                launch.attach(created, tb=tb)
            return 0
        if yolo:
            print("kilix-rollout-resume: resuming with "
                  f"{config.yolo_flag(chosen.provider)}", file=sys.stderr)
        launch.hand_over(
            chosen, cwd=cwd, yolo=yolo, executable=executable,
            force_live=force_live, fork=fork, permission_mode=permission_mode,
            model=agent_model, prompt=prompt)
        return 0
    if command == "restore":
        tb, no_log = _backend_options(arguments)
        dry_run = _take_flag(arguments, "--dry-run")
        force_live = _take_flag(arguments, "--force-live")
        fork = _take_flag(arguments, "--fork")
        permission_mode = _take_value(
            arguments, "--permission-mode", default="")
        agent_model = _take_value(arguments, "--model", default="")
        prompt = _take_value(arguments, "--prompt", default="")
        executables = {}
        for key in ("claude", "codex", "kimi"):
            value = _take_value(arguments, f"--{key}", default="")
            if value:
                executables[key] = _configured_program(value, key)
        state = _take_value(arguments, "--state", default="candidates")
        query = _take_value(arguments, "--query", "-q", default="")
        limit = _take_value(arguments, "--limit", default=10, convert=int)
        gap = _take_value(
            arguments, "--gap", "--interval", default=None, convert=float)
        if gap is None:
            gap = config.launch_gap()
        if limit < 1:
            raise RuntimeError("--limit must be greater than zero")
        if gap < pacing.MINIMUM_INTERVAL:
            raise RuntimeError(
                f"--gap must be at least {pacing.MINIMUM_INTERVAL:.0f} seconds")
        strict_targets = bool(arguments)
        if arguments:
            sessions = discover_sessions(
                window=0.0,
                selectors=tuple(arguments),
            )
            selected = []
            seen = set()
            for selector in arguments:
                item = resolve(sessions, selector)
                if item.session_id not in seen:
                    selected.append(item)
                    seen.add(item.session_id)
        else:
            sessions = discover_sessions()
            selected = _filter_sessions(sessions, state=state, query=query)
        return _cmd_restore(
            selected, limit, gap, yolo, dry_run=dry_run, as_json=as_json,
            tb=tb, no_log=no_log, executables=executables,
            force_live=force_live, fork=fork,
            permission_mode=permission_mode, agent_model=agent_model,
            prompt=prompt, assume_yes=assume_yes,
            strict_targets=strict_targets)
    tui_tb = ""
    tui_no_log = False
    tui_executables: dict[str, str] = {}
    tui_gap: float | None = None
    if command in ("", "tui"):
        tui_tb, tui_no_log = _backend_options(arguments)
        for key in ("claude", "codex", "kimi"):
            value = _take_value(arguments, f"--{key}", default="")
            if value:
                tui_executables[key] = _configured_program(value, key)
        tui_gap = _take_value(
            arguments, "--gap", "--interval", default=None, convert=float)
        if tui_gap is not None and tui_gap < pacing.MINIMUM_INTERVAL:
            raise UsageError(
                f"--interval must be at least "
                f"{pacing.MINIMUM_INTERVAL:.0f} seconds")
        if arguments:
            raise UsageError(
                "unexpected tui argument(s): " + " ".join(arguments))
    if command and command != "tui":
        print(f"kilix-rollout-resume: unknown command '{command}'", file=sys.stderr)
        return 2

    if command != "tui" and not sys.stdout.isatty():
        sessions = discover_sessions()
        _print_sessions(sessions, as_json, no_header=no_header)
        return 0

    state_options = {
        "roots": roots,
        "include_archived": include_archived,
        "include_orphans": include_orphans,
        "tb": tui_tb,
        "no_log": tui_no_log,
        "initial_since": since if (all_time or since_text) else None,
        "executables": tui_executables,
        "gap": tui_gap,
        "provider_keys": tuple(agent_filter) if agent_filter else None,
    }
    destination = screenshot or app.screenshot_argv(sys.argv)
    if destination:
        with open(destination, "w", encoding="utf-8") as output_file:
            output_file.write(
                app.render_to_text(render, State(**state_options)))
        return 0
    return app.run(render, State(**state_options), handle=handle)


def _runtime_error(error: RuntimeError) -> ResumeError:
    text = str(error)
    lowered = text.casefold()
    if ("no session matches" in lowered
            or "working directory no longer exists" in lowered
            or "could not find" in lowered):
        return NotFoundError(text)
    if ("ambiguous" in lowered or "already exists" in lowered
            or "still owned" in lowered or "still open" in lowered):
        return ConflictError(text)
    if ("tmux backend" in lowered or "tmux could not" in lowered
            or "tmux failed" in lowered or "no tmux server" in lowered):
        return BackendError(text)
    if "rate-limit pacing" in lowered:
        return PacingError(text)
    if (text.startswith("usage:") or text.startswith("unexpected ")
            or text.startswith("invalid ")
            or text.startswith("unknown ")
            or text.startswith("--")
            or " requires a value" in lowered
            or " cannot be used together" in lowered
            or " applies only to " in lowered
            or "must be at least" in lowered):
        return UsageError(text)
    return ResumeError(text)


def _legacy_provider_data(value, provider_key: str):
    """Add the retired provider tools' field names to envelope payloads."""
    if isinstance(value, list):
        return [
            _legacy_provider_data(item, provider_key)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    result = {
        key: _legacy_provider_data(item, provider_key)
        for key, item in value.items()
    }
    if "session_id" in result and "legacy_state" in result:
        result["state"] = result["legacy_state"]
        result.setdefault(
            "label",
            result.get("title") or result.get("last_user_message")
            or "(no recorded prompt)",
        )
        if provider_key == "claude":
            result.setdefault(
                "last_assistant_message",
                result.get("last_agent_message", ""),
            )
            result.setdefault("last_record", result.get("last_turn_event", ""))
            path = result.get("path")
            result.setdefault(
                "project_dir",
                str(Path(str(path)).parent) if path else None,
            )
        elif provider_key == "codex":
            result.setdefault("source", result.get("entrypoint", ""))
            result.setdefault("cli_version", result.get("version", ""))
    if result.get("dry_run") and "gap" in result:
        result.setdefault("interval", result["gap"])
    if result.get("dry_run") and isinstance(result.get("plans"), list):
        result.setdefault("launches", result["plans"])
        result.setdefault("total", len(result["plans"]))
    if "tmux_command" in result:
        result.setdefault("tb_command", result["tmux_command"])
    command = result.get("command")
    if isinstance(command, list):
        if provider_key == "claude":
            result.setdefault(
                "unsafe", "--dangerously-skip-permissions" in command)
        elif provider_key == "codex":
            result.setdefault("yolo", "--yolo" in command)
    return result


def entrypoint(argv: list[str]) -> int:
    """Run the CLI, optionally wrapping all output in a stable JSON envelope."""
    arguments = list(argv)
    provider_mode = ""
    provider_command = ""
    if arguments and arguments[0] in ("claude", "codex"):
        provider_mode = arguments.pop(0)
        known_commands = {
            "list", "ls", "show", "resume", "restore", "tui", "doctor",
            "prune", "configure", "install-launcher", "uninstall-launcher",
        }
        provider_command = next(
            (token for token in arguments if token in known_commands), "")
        if "--agent" in arguments:
            failure = UsageError(
                f"{provider_mode} mode cannot be combined with --agent")
            print(f"kilix-rollout-resume: {failure}", file=sys.stderr)
            return failure.exit_status
        if (provider_command in ("list", "ls", "restore", "tui")
                or (not provider_command
                    and not any(flag in arguments for flag in (
                        "-h", "--help", "--version", "help", "version")))):
            if "--since" not in arguments and "--all-time" not in arguments:
                arguments.extend((
                    "--since", "7d" if provider_mode == "claude" else "1h"))
        # Both retired tools always created a persistent tmux session. Keep
        # that default inside their compatibility namespaces while the unified
        # command retains its direct-handover default.
        if provider_command == "resume" and "--detached" not in arguments:
            arguments.append("--detached")
        arguments.extend(("--agent", provider_mode))
        # The provider-specific tools used the stable envelope for --json.
        # Preserve that exact machine-facing behavior inside their namespace.
        if "--json" in arguments and "--envelope" not in arguments:
            arguments.remove("--json")
            arguments.append("--envelope")
    envelope = _take_flag(arguments, "--envelope", "--json-envelope")
    if not envelope:
        try:
            return main(arguments)
        except KeyboardInterrupt:
            return 130
        except ResumeError as error:
            print(f"kilix-rollout-resume: {error}", file=sys.stderr)
            return error.exit_status
        except KeyError as error:
            failure = UsageError(str(error).strip("'"))
            print(f"kilix-rollout-resume: {failure}", file=sys.stderr)
            return failure.exit_status
        except RuntimeError as error:
            failure = _runtime_error(error)
            print(f"kilix-rollout-resume: {failure}", file=sys.stderr)
            return failure.exit_status

    if "--json" not in arguments:
        arguments.append("--json")
    output = io.StringIO()
    try:
        with redirect_stdout(output):
            status = main(arguments)
        raw = output.getvalue().strip()
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = raw
        if provider_mode:
            data = _legacy_provider_data(data, provider_mode)
        payload: dict[str, object] = {
            "ok": status == 0,
            "data": data,
        }
        if status != 0:
            payload["code"] = "EFAIL"
            payload["error"] = f"command exited {status}"
        print(json.dumps(payload, indent=2))
        return status
    except KeyboardInterrupt:
        print(json.dumps({
            "ok": False, "code": "EINTERRUPTED", "error": "interrupted",
        }, indent=2))
        return 130
    except ResumeError as error:
        failure = error
    except KeyError as error:
        failure = UsageError(str(error).strip("'"))
    except RuntimeError as error:
        failure = _runtime_error(error)
    print(json.dumps({
        "ok": False,
        "code": failure.code,
        "error": str(failure),
    }, indent=2))
    return failure.exit_status


if __name__ == "__main__":
    raise SystemExit(entrypoint(sys.argv[1:]))
