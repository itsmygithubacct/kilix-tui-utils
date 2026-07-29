"""kilix-rollout-resume — recover coding-agent sessions whose terminal is gone.

Claude Code, Codex, and Kimi Code each persist a conversation as it happens and
each can reload one by ID, so a session whose window closed is still on disk.
This lists all three together, hands the current Kilix tab over to the one you
pick, and — because a picker is no use without the agent that owns the
transcript — installs or updates the agents themselves.
"""
from __future__ import annotations

import curses
import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap  # noqa: E402
from kilix_rollout import launch, manage, menu, model, providers  # noqa: E402
from kilix_rollout.model import RANGES, Session  # noqa: E402

VIEWS = ("candidates", "cut-off", "idle", "live", "all")
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
        raise SystemExit(f"kilix-rollout-resume: invalid duration '{text}'") from None
    if amount <= 0:
        raise SystemExit("kilix-rollout-resume: duration must be greater than zero")
    return amount * (unit or 1)


# ── selection ────────────────────────────────────────────────────────────────

def visible(sessions: list[Session], view: str, agent: str) -> list[Session]:
    chosen = sessions
    if agent != "all":
        chosen = [item for item in chosen if item.provider == agent]
    if view == "candidates":
        return [item for item in chosen if item.resumable]
    if view == "all":
        return chosen
    return [item for item in chosen if item.state == view]


def resolve(sessions: list[Session], selector: str) -> Session:
    """Find one session by ID or unique ID prefix."""
    needle = selector.strip().casefold()
    if not needle:
        raise SystemExit("kilix-rollout-resume: a session ID is required")
    matches = [item for item in sessions if item.session_id.casefold().startswith(needle)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"kilix-rollout-resume: no session matches '{selector}'")
    listed = ", ".join(item.short_id for item in matches[:6])
    raise SystemExit(f"kilix-rollout-resume: '{selector}' is ambiguous: {listed}")


# ── state ────────────────────────────────────────────────────────────────────

class State:
    def __init__(self) -> None:
        self.pane = 0
        self.view = 0
        self.agent = 0
        self.window = model.DEFAULT_RANGE
        self.selected = 0
        self.agent_row = 0
        self.offset = 0
        self.status = ""
        self.sessions: list[Session] = []
        self.agents: list[dict] = []
        self.refresh()
        # Nothing lost recently is the common case on a machine that has been
        # idle; widen once rather than showing an empty list.
        while not self.sessions and self.window < len(RANGES) - 1:
            self.window += 1
            self.refresh()
            if self.sessions:
                self.status = (f"Nothing in the last {RANGES[self.window - 1][0]}; "
                               f"showing the last {RANGES[self.window][0]}.")
        if not any(row["installed"] for row in self.agents):
            self.pane = 1
            self.status = "No coding agent found — press Enter to install one."

    def refresh(self) -> None:
        self.sessions = providers.discover(since=RANGES[self.window][1])
        self.agents = manage.status(providers.PROVIDERS)
        self.selected = min(self.selected, max(0, len(self.rows) - 1))

    @property
    def rows(self) -> list[Session]:
        return visible(self.sessions, VIEWS[self.view], AGENTS[self.agent])

    @property
    def current(self) -> Session | None:
        rows = self.rows
        return rows[self.selected] if rows else None

    @property
    def current_agent(self):
        return providers.PROVIDERS[self.agent_row]


# ── rendering ────────────────────────────────────────────────────────────────

def _row_text(item: Session, width: int) -> str:
    prefix = (f"{item.state.upper():<8}{item.provider:<7}{item.age():>4}  "
              f"{item.project[:16]:<16}  {item.short_id:<13}  ")
    return prefix + (item.title or "(no recorded prompt)")[:max(0, width - len(prefix))]


def render(surface, state: State) -> None:
    height, width = surface.getmaxyx()
    pane = PANES[state.pane]
    surface.addstr(0, 0, (
        f" kilix-rollout-resume   pane:{pane}   view:{VIEWS[state.view]}"
        f"   agent:{AGENTS[state.agent]}   range:{RANGES[state.window][0]} "
    )[:width - 1], curses.A_REVERSE)

    if pane == "agents":
        _render_agents(surface, state, height, width)
    else:
        _render_sessions(surface, state, height, width)

    surface.addstr(height - 2, 0, state.status[:width - 1], curses.A_DIM)
    footer = (" Enter resume · x tmux · Tab agents · v view · a agent · t range · r refresh · q quit"
              if pane == "sessions" else
              " Enter install/update · Tab sessions · m sync menu · r refresh · ? help · q quit")
    surface.addstr(height - 1, 0, footer[:width - 1], curses.A_BOLD)


def _render_sessions(surface, state: State, height: int, width: int) -> None:
    surface.addstr(2, 0, ("  STATE   AGENT   AGE  PROJECT           "
                          "SESSION        CONVERSATION")[:width - 1], curses.A_BOLD)
    rows = state.rows
    body = max(1, height - 6)
    if state.selected < state.offset:
        state.offset = state.selected
    if state.selected >= state.offset + body:
        state.offset = state.selected - body + 1
    state.offset = max(0, min(state.offset, max(0, len(rows) - body)))

    if not rows:
        surface.addstr(4, 2, "(no sessions match this view)")
    for line, index in enumerate(range(state.offset, min(len(rows), state.offset + body)), start=3):
        item = rows[index]
        attribute = curses.A_REVERSE if index == state.selected else 0
        surface.addstr(line, 1, _row_text(item, width - 2), attribute)

    chosen = state.current
    if chosen is not None:
        surface.addstr(height - 3, 0,
                       f" {chosen.cwd or '(no working directory)'}"[:width - 1],
                       curses.A_DIM)


def _render_agents(surface, state: State, height: int, width: int) -> None:
    surface.addstr(2, 0, "  AGENT         COMMAND   STATUS"[:width - 1], curses.A_BOLD)
    for line, row in enumerate(state.agents, start=3):
        attribute = curses.A_REVERSE if line - 3 == state.agent_row else 0
        mark = "installed" if row["installed"] else "NOT INSTALLED"
        text = f"{row['label']:<14}{row['command']:<10}{mark}"
        surface.addstr(line, 1, text[:width - 2], attribute)

    item = state.current_agent
    row = state.agents[state.agent_row]
    if row["installed"]:
        surface.addstr(height - 4, 1, f"Update with: {' '.join(item.update_argv)}"[:width - 2],
                       curses.A_DIM)
        surface.addstr(height - 3, 1, f"Installed at: {row['path']}"[:width - 2], curses.A_DIM)
    else:
        surface.addstr(height - 4, 1, f"Install with: {item.install_shell}"[:width - 2],
                       curses.A_DIM)
        surface.addstr(height - 3, 1, f"Documented at: {item.install_source}"[:width - 2],
                       curses.A_DIM)


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
    launch.hand_over(chosen)          # replaces this process
    return False


def _resume_tmux(state: State) -> bool:
    chosen = state.current
    if chosen is None or chosen.state == "live":
        state.status = "Select a resumable session first."
        return True
    try:
        name = launch.start_detached(chosen)
    except RuntimeError as error:
        state.status = str(error)
        return True
    state.status = f"Started detached tmux session '{name}'. Attach with: tmux attach -t {name}"
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
    state.status = ("Enter hands this pane to the agent · x starts it detached in tmux · "
                    "t widens the time range · Tab opens the agent list, where Enter "
                    "installs a missing agent or updates an installed one.")
    return True


def handle(key: int, state: State) -> bool:
    if keymap.is_quit(key):
        return False
    if key == ord("\t"):
        state.pane = (state.pane + 1) % len(PANES)
        return True
    if keymap.is_refresh(key):
        state.refresh()
        state.status = f"{len(state.sessions)} session(s) found."
        return True
    if keymap.is_help(key) or key == ord("?"):
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
    if key in (ord("\n"), ord("\r")):
        return _resume_here(state)
    if key == ord("x"):
        return _resume_tmux(state)
    if key == ord("t"):
        state.window = (state.window + 1) % len(RANGES)
        state.selected = 0
        state.refresh()
        state.status = (f"Showing the last {RANGES[state.window][0]}: "
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
    return True


# ── command line ─────────────────────────────────────────────────────────────

def _print_sessions(sessions: list[Session], as_json: bool) -> None:
    if as_json:
        print(json.dumps([{
            "provider": item.provider, "session_id": item.session_id,
            "state": item.state, "project": item.project, "cwd": item.cwd,
            "title": item.title, "updated": item.updated, "path": item.path,
        } for item in sessions], indent=2))
        return
    if not sessions:
        print("(no saved sessions)")
        return
    print("STATE   AGENT   AGE  PROJECT           SESSION        CONVERSATION")
    for item in sessions:
        print(f"{item.state.upper():<8}{item.provider:<7}{item.age():>4}  "
              f"{item.project[:16]:<16}  {item.short_id:<13}  "
              f"{item.title or '(no recorded prompt)'}")


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


def _cmd_restore(sessions: list[Session], limit: int, gap: float) -> int:
    chosen = [item for item in sessions if item.resumable][:limit]
    if not chosen:
        print("(nothing to restore)")
        return 0
    chosen.reverse()          # oldest first, so names follow the timeline
    span = gap * max(0, len(chosen) - 1)
    print(f"Restoring {len(chosen)} session(s), {gap:.0f}s apart "
          f"(about {span / 60:.0f}m total).")
    results = launch.restore_all(chosen, gap=gap)
    for result in results:
        item = result["session"]
        mark = "started" if result["ok"] else "failed "
        print(f"  {mark} {item.provider:<7}{item.short_id}  {result['detail']}")
    return 0 if any(result["ok"] for result in results) else 1


def main(argv: list[str]) -> int:
    arguments = list(argv)
    as_json = "--json" in arguments
    assume_yes = "--yes" in arguments or "-y" in arguments
    arguments = [item for item in arguments if item not in ("--json", "--yes", "-y")]

    agent_filter = None
    if "--agent" in arguments:
        index = arguments.index("--agent")
        agent_filter = [arguments[index + 1]]
        del arguments[index:index + 2]

    since = RANGES[model.DEFAULT_RANGE][1]
    if "--all-time" in arguments:
        since = 0.0
        arguments.remove("--all-time")
    if "--since" in arguments:
        index = arguments.index("--since")
        since = parse_duration(arguments[index + 1])
        del arguments[index:index + 2]

    command = arguments[0] if arguments else ""

    if command in ("-h", "--help", "help"):
        print(__doc__.strip())
        print("\nCommands: list, resume <id>, restore, install <agent>, "
              "update <agent>, status, sync-menu"
              "\nOptions:  --agent <name>, --since <90m|24h|7d>, --all-time, "
              "--json, --yes"
              "\nAgents:   " + ", ".join(item.key for item in providers.PROVIDERS))
        return 0
    if command == "status":
        return _cmd_status(as_json)
    if command == "install":
        if len(arguments) < 2:
            print("usage: kilix-rollout-resume install <agent>", file=sys.stderr)
            return 2
        return _cmd_install(arguments[1], assume_yes)
    if command == "update":
        if len(arguments) < 2:
            print("usage: kilix-rollout-resume update <agent>", file=sys.stderr)
            return 2
        return _cmd_update(arguments[1])
    if command == "sync-menu":
        result = menu.sync(providers.PROVIDERS)
        for path in result["written"]:
            print(f"wrote   {path}")
        for path in result["removed"]:
            print(f"removed {path}")
        return 0

    sessions = providers.discover(agent_filter, since=since)

    if command == "list":
        _print_sessions(sessions, as_json)
        return 0
    if command == "resume":
        if len(arguments) < 2:
            print("usage: kilix-rollout-resume resume <session-id>", file=sys.stderr)
            return 2
        chosen = resolve(sessions, arguments[1])
        if chosen.state == "live":
            print(f"kilix-rollout-resume: that session is still running "
                  f"(PID {', '.join(str(pid) for pid in chosen.pids)})", file=sys.stderr)
            return 4
        launch.hand_over(chosen)
        return 0
    if command == "restore":
        limit = 10
        if "--limit" in arguments:
            limit = int(arguments[arguments.index("--limit") + 1])
        gap = launch.LAUNCH_GAP
        if "--gap" in arguments:
            gap = float(arguments[arguments.index("--gap") + 1])
        return _cmd_restore(sessions, limit, gap)
    if command:
        print(f"kilix-rollout-resume: unknown command '{command}'", file=sys.stderr)
        return 2

    if not sys.stdout.isatty():
        _print_sessions(sessions, as_json)
        return 0

    destination = app.screenshot_argv(sys.argv)
    if destination:
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, State()))
        return 0
    return app.run(render, State(), handle=handle)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, KeyError) as error:
        print(f"kilix-rollout-resume: {error}", file=sys.stderr)
        raise SystemExit(1)
