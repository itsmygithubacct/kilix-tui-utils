"""kilix-rollout-resume — recover coding-agent sessions whose terminal is gone.

Claude Code, Codex, and Kimi Code each persist a conversation as it happens and
each can reload one by ID, so a session whose window closed is still on disk.
This lists all three together, hands the current Kilix tab over to the one you
pick, and — because a picker is no use without the agent that owns the
transcript — installs or updates the agents themselves.
"""
from __future__ import annotations

import curses
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap  # noqa: E402
from kilix_rollout import (  # noqa: E402
    claude, codex, config, kimi, launch, liveness, manage, menu, model, pacing,
    providers,
)
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
    """Find one session by ID/prefix, transcript filename, or full path."""
    needle = selector.strip().casefold()
    if not needle:
        raise SystemExit("kilix-rollout-resume: a session ID is required")
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
        self.yolo = config.yolo_default()
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
        f"   agent:{AGENTS[state.agent]}   range:{RANGES[state.window][0]}"
        f"{'   YOLO' if state.yolo else ''} "
    )[:width - 1], curses.A_REVERSE)

    if pane == "agents":
        _render_agents(surface, state, height, width)
    else:
        _render_sessions(surface, state, height, width)

    surface.addstr(height - 2, 0, state.status[:width - 1], curses.A_DIM)
    footer = (" Enter resume · x tmux · y yolo · Tab agents · v view · a agent · t range · q quit"
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
    launch.hand_over(chosen, yolo=state.yolo)   # replaces this process
    return False


def _resume_tmux(state: State) -> bool:
    chosen = state.current
    if chosen is None or chosen.state == "live":
        state.status = "Select a resumable session first."
        return True
    try:
        pacer = pacing.LaunchPacer(interval=config.launch_gap())
        remaining = pacer.remaining()
        if remaining:
            state.status = (
                f"Waiting {remaining:.0f}s for the shared rate-limit guard…")
        name = launch.start_detached(
            chosen, yolo=state.yolo, pacer=pacer)
    except RuntimeError as error:
        state.status = str(error)
        return True
    state.status = f"Started detached tmux session '{name}'. Attach with: tmux attach -t {name}"
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
    if key == ord("y"):
        return _toggle_yolo(state)
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

def _session_dict(item: Session) -> dict[str, object]:
    try:
        size = os.path.getsize(item.path)
    except OSError:
        size = 0
    return {
        "provider": item.provider,
        "session_id": item.session_id,
        "state": item.state,
        "resumable": item.resumable,
        "project": item.project,
        "cwd": item.cwd,
        "cwd_exists": bool(item.cwd and os.path.isdir(item.cwd)),
        "title": item.title,
        "updated": item.updated,
        "updated_at": datetime.fromtimestamp(
            item.updated, timezone.utc).isoformat(),
        "path": item.path,
        "size_bytes": size,
        "live_pids": list(item.pids),
    }


def _print_sessions(sessions: list[Session], as_json: bool) -> None:
    if as_json:
        print(json.dumps([_session_dict(item) for item in sessions], indent=2))
        return
    if not sessions:
        print("(no saved sessions)")
        return
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
    if state not in VIEWS:
        raise RuntimeError(
            f"unknown state '{state}'; use " + ", ".join(VIEWS))
    chosen = visible(sessions, state, "all")
    needle = query.strip().casefold()
    if not needle:
        return chosen
    return [
        item for item in chosen
        if needle in "\n".join((
            item.provider, item.session_id, item.project, item.cwd,
            item.title, item.path)).casefold()
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
        ("Resumable", "yes" if record["resumable"] else "no"),
        ("Updated", record["updated_at"]),
        ("Project", record["project"]),
        ("Working directory", record["cwd"] or "-"),
        ("Directory exists", "yes" if record["cwd_exists"] else "no"),
        ("Transcript", record["path"]),
        ("Size", f"{record['size_bytes']} bytes"),
        ("Live PIDs", ", ".join(map(str, record["live_pids"])) or "-"),
        ("Conversation", record["title"] or "-"),
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
) -> int:
    chosen = [item for item in sessions if item.resumable][:limit]
    if not chosen:
        print("[]" if as_json else "(nothing to restore)")
        return 0
    chosen.reverse()          # oldest first, so names follow the timeline
    span = gap * max(0, len(chosen) - 1)
    pacer = pacing.LaunchPacer(interval=gap)
    if dry_run:
        taken = launch.tmux_sessions()
        plans = []
        for index, item in enumerate(chosen):
            plan = launch.resume_plan(
                item, detached=True, yolo=yolo, taken=taken)
            taken.add(str(plan["tmux_name"]))
            plans.append({
                "order": index + 1,
                "wait_before": gap if index else round(pacer.remaining(), 3),
                **plan,
            })
        if as_json:
            print(json.dumps({"dry_run": True, "gap": gap, "plans": plans}, indent=2))
        else:
            print(f"Dry run: {len(plans)} session(s), {gap:.0f}s apart "
                  f"(about {span / 60:.0f}m total).")
            for plan in plans:
                print(f"  {plan['order']:>2}. {plan['provider']:<7}"
                      f"{str(plan['session_id'])[:13]} -> "
                      f"{plan['tmux_name']}  {plan['command_text']}")
        return 0
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
        chosen, gap=gap, yolo=yolo, pacer=pacer, on_wait=report_wait)
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
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate.absolute())
    found = shutil.which(value)
    if found:
        return str(Path(found).absolute())
    raise RuntimeError(f"could not find {label}: {value}")


def _cmd_configure(arguments: list[str], as_json: bool) -> int:
    updates: dict[str, object] = {}
    for key in ("tmux", "claude", "codex", "kimi"):
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


def _doctor_rows() -> list[dict[str, object]]:
    applications = Path(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
    ) / "applications"
    binary = Path(
        os.environ.get("XDG_BIN_HOME", str(Path.home() / ".local/bin"))
    ) / "kilix-rollout-resume"
    roots = (
        ("Claude transcripts", Path(claude.home()) / "projects"),
        ("Codex transcripts", Path(codex.home()) / "sessions"),
        ("Kimi transcripts", Path(kimi.home()) / "sessions"),
    )
    rows: list[dict[str, object]] = [
        {"check": label, "value": str(path), "ok": path.exists(),
         "required": False}
        for label, path in roots
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
    rows.extend((
        {"check": "tmux", "value": tmux or config.configured_program("tmux", "tmux"),
         "ok": bool(tmux), "required": True},
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


def _cmd_doctor(as_json: bool) -> int:
    rows = _doctor_rows()
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
        "  install|update <agent>  manage an agent; status reports all agents\n"
        "  sync-menu               refresh Kilix-95 entries\n"
        "\nSelection: --agent <name>, --since <90m|24h|7d>, --all-time, "
        "--state <state>, --query <text>, --limit <n>\n"
        "Resume:    --detached, --attach, --name <name>, --cwd <path>, "
        "--dry-run, --force-live\n"
        "Claude:    --fork, --permission-mode <mode>, --model <name>, "
        "--prompt <text>\n"
        "Safety:    --yolo, --no-yolo; JSON: --json\n"
        "Agents:    " + ", ".join(item.key for item in providers.PROVIDERS)
    )


def main(argv: list[str]) -> int:
    arguments = list(argv)
    as_json = _take_flag(arguments, "--json")
    assume_yes = _take_flag(arguments, "--yes", "-y")
    yolo = config.yolo_default()
    if _take_flag(arguments, "--yolo", "--dangerously-skip-permissions"):
        yolo = True
    if _take_flag(arguments, "--no-yolo"):
        yolo = False

    agent = _take_value(arguments, "--agent", default="")
    agent_filter = [agent] if agent and agent != "all" else None
    since = RANGES[model.DEFAULT_RANGE][1]
    if _take_flag(arguments, "--all-time"):
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
        return _cmd_doctor(as_json)
    if command == "prune":
        if arguments:
            raise RuntimeError("prune takes no positional arguments")
        return _cmd_prune(as_json)

    sessions = providers.discover(agent_filter, since=since)

    if command in ("list", "ls"):
        state = _take_value(arguments, "--state", default="all")
        query = _take_value(arguments, "--query", "-q", default="")
        limit = _take_value(arguments, "--limit", default=100, convert=int)
        if limit < 1:
            raise RuntimeError("--limit must be greater than zero")
        if arguments:
            raise RuntimeError("unexpected list argument(s): " + " ".join(arguments))
        _print_sessions(
            _filter_sessions(sessions, state=state, query=query)[:limit], as_json)
        return 0
    if command == "show":
        if len(arguments) != 1:
            raise RuntimeError("usage: kilix-rollout-resume show <session-id>")
        return _cmd_show(resolve(sessions, arguments[0]), as_json)
    if command == "resume":
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
            arguments, "--gap", "--interval", default=config.launch_gap(),
            convert=float)
        if gap < pacing.MINIMUM_INTERVAL:
            raise RuntimeError(
                f"--gap must be at least {pacing.MINIMUM_INTERVAL:.0f} seconds")
        executable_options = {
            key: _take_value(arguments, f"--{key}", default="")
            for key in ("claude", "codex", "kimi")
        }
        if len(arguments) != 1:
            raise RuntimeError("usage: kilix-rollout-resume resume <session-id>")
        chosen = resolve(sessions, arguments[0])
        wrong = [key for key, value in executable_options.items()
                 if value and key != chosen.provider]
        if wrong:
            raise RuntimeError(
                f"--{wrong[0]} does not apply to a {chosen.provider} session")
        executable = executable_options[chosen.provider]
        if executable:
            executable = _configured_program(executable, chosen.provider)
        detached = detached or attach or bool(name)
        if attach and as_json:
            raise RuntimeError("--attach and --json cannot be used together")
        if as_json and not (detached or dry_run):
            raise RuntimeError(
                "--json with a direct handover requires --dry-run or --detached")
        plan = launch.resume_plan(
            chosen, detached=detached, name=name, cwd=cwd, yolo=yolo,
            executable=executable, force_live=force_live, fork=fork,
            permission_mode=permission_mode, model=agent_model, prompt=prompt)
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
                prompt=prompt, pacer=pacer, on_wait=report_wait)
            if as_json:
                print(json.dumps({"created": True, **plan}, indent=2))
            else:
                print(f"Resumed {chosen.short_id} as tmux session "
                      f"'{created}' in {plan['cwd']}")
            if attach:
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise RuntimeError("attaching requires an interactive terminal")
                launch.attach(created)
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
        dry_run = _take_flag(arguments, "--dry-run")
        state = _take_value(arguments, "--state", default="candidates")
        query = _take_value(arguments, "--query", "-q", default="")
        limit = _take_value(arguments, "--limit", default=10, convert=int)
        gap = _take_value(
            arguments, "--gap", "--interval", default=config.launch_gap(),
            convert=float)
        if limit < 1:
            raise RuntimeError("--limit must be greater than zero")
        if gap < pacing.MINIMUM_INTERVAL:
            raise RuntimeError(
                f"--gap must be at least {pacing.MINIMUM_INTERVAL:.0f} seconds")
        if arguments:
            selected = []
            seen = set()
            for selector in arguments:
                item = resolve(sessions, selector)
                if item.session_id not in seen:
                    selected.append(item)
                    seen.add(item.session_id)
        else:
            selected = _filter_sessions(sessions, state=state, query=query)
        return _cmd_restore(
            selected, limit, gap, yolo, dry_run=dry_run, as_json=as_json)
    if command and command != "tui":
        print(f"kilix-rollout-resume: unknown command '{command}'", file=sys.stderr)
        return 2

    if command != "tui" and not sys.stdout.isatty():
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
