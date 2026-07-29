"""Start a recovered session.

Two ways, because they answer different needs. Handing the current pane over to
the agent is the natural one on a terminal desktop: the Kilix tab you opened
the picker in becomes the coding session. Detached tmux sessions are for
restoring several at once, where you want them all running and none of them
attached.

Batch restores are spaced out. Several agents waking up together all draw on
the same account rate limit, and the fastest way to get throttled is to start
them back to back.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time

from . import config
from .model import Session
from .providers import Provider, provider

#: Seconds between queued launches. Restoring a batch means several agents
#: begin consuming tokens against one account-wide limit.
LAUNCH_GAP = config.DEFAULT_GAP

PERMISSION_MODES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "dontAsk",
    "manual",
    "plan",
)


def resume_command(
    session: Session,
    *,
    yolo: bool = False,
    executable: str = "",
    fork: bool = False,
    permission_mode: str = "",
    model: str = "",
    prompt: str = "",
) -> list[str]:
    """Build the exact agent command, including Claude-only resume options."""
    item = provider(session.provider)
    argv = item.resume_argv(session, yolo=yolo)
    argv[0] = executable or config.configured_program(item.key, item.command)

    claude_options = bool(fork or permission_mode or model or prompt)
    if session.provider != "claude" and claude_options:
        raise RuntimeError(
            "--fork, --permission-mode, --model, and --prompt apply only to Claude")
    if session.provider == "claude":
        if permission_mode and permission_mode not in PERMISSION_MODES:
            raise RuntimeError(
                f"unknown permission mode '{permission_mode}'; use "
                + ", ".join(PERMISSION_MODES))
        # The provider has already added the resume ID and optional yolo flag.
        # Claude accepts the remaining modifiers after those arguments.
        if fork:
            argv.append("--fork-session")
        if permission_mode:
            argv.extend(("--permission-mode", permission_mode))
        if model:
            argv.extend(("--model", model))
        if prompt:
            argv.append(prompt)
    return argv


def working_directory(session: Session, override: str = "") -> str:
    """Return the directory to resume in, or raise if it is gone."""
    cwd = os.path.abspath(os.path.expanduser(override)) if override else session.cwd or ""
    if not cwd:
        raise RuntimeError("this session has no recorded working directory")
    if not os.path.isdir(cwd):
        raise RuntimeError(f"working directory no longer exists: {cwd}")
    return cwd


def check_installed(session: Session, executable: str = "") -> Provider:
    from . import manage

    item = provider(session.provider)
    if executable:
        candidate = os.path.abspath(os.path.expanduser(executable))
        installed = (candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK)
                     else shutil.which(executable) or "")
    else:
        installed = manage.installed(item)
    if not installed:
        raise RuntimeError(
            f"{item.label} is not installed; install it before resuming")
    return item


def _check_live(session: Session, force_live: bool) -> None:
    if session.state == "live" and not force_live:
        detail = f" (PID {', '.join(str(pid) for pid in session.pids)})" if session.pids else ""
        raise RuntimeError(
            f"session is still owned by a running {session.provider} process{detail}; "
            "use --force-live only if that process is stale")


def hand_over(
    session: Session,
    *,
    yolo: bool = False,
    cwd: str = "",
    executable: str = "",
    force_live: bool = False,
    fork: bool = False,
    permission_mode: str = "",
    model: str = "",
    prompt: str = "",
) -> None:
    """Replace this process with the resumed agent. Does not return."""
    _check_live(session, force_live)
    check_installed(session, executable)
    chosen_cwd = working_directory(session, cwd)
    argv = resume_command(
        session, yolo=yolo, executable=executable, fork=fork,
        permission_mode=permission_mode, model=model, prompt=prompt)
    os.chdir(chosen_cwd)
    os.execvp(argv[0], argv)


def tmux_name(session: Session, taken=()) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", session.project).strip("_-")
    base = f"{session.provider}_{base or 'session'}"[:60]
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


def requested_tmux_name(session: Session, requested: str, taken=()) -> str:
    """Sanitize an explicit name and reject collisions instead of renaming it."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", requested.strip())
    name = re.sub(r"_+", "_", name).strip("_-")[:64]
    name = name or f"{session.provider}_session"
    if name in taken:
        raise RuntimeError(f"tmux session '{name}' already exists")
    return name


def tmux_sessions(*, runner=subprocess.run) -> set[str]:
    tmux = config.configured_program("tmux", "tmux")
    try:
        result = runner([tmux, "list-sessions", "-F", "#{session_name}"],
                        capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()          # usually just "no server running"
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def tmux_argv(
    session: Session,
    name: str,
    *,
    yolo: bool = False,
    cwd: str = "",
    executable: str = "",
    fork: bool = False,
    permission_mode: str = "",
    model: str = "",
    prompt: str = "",
) -> list[str]:
    tmux = config.configured_program("tmux", "tmux")
    chosen_cwd = working_directory(session, cwd)
    command = resume_command(
        session, yolo=yolo, executable=executable, fork=fork,
        permission_mode=permission_mode, model=model, prompt=prompt)
    return [tmux, "new-session", "-d", "-s", name, "-c", chosen_cwd, *command]


def resume_plan(
    session: Session,
    *,
    detached: bool = False,
    name: str = "",
    cwd: str = "",
    yolo: bool = False,
    executable: str = "",
    force_live: bool = False,
    fork: bool = False,
    permission_mode: str = "",
    model: str = "",
    prompt: str = "",
    taken=None,
    runner=subprocess.run,
) -> dict[str, object]:
    """Validate a resume and describe it without launching anything."""
    _check_live(session, force_live)
    check_installed(session, executable)
    chosen_cwd = working_directory(session, cwd)
    command = resume_command(
        session, yolo=yolo, executable=executable, fork=fork,
        permission_mode=permission_mode, model=model, prompt=prompt)
    chosen_name = ""
    tmux_command: list[str] = []
    if detached:
        existing = tmux_sessions(runner=runner) if taken is None else set(taken)
        chosen_name = (requested_tmux_name(session, name, existing)
                       if name else tmux_name(session, existing))
        tmux_command = tmux_argv(
            session, chosen_name, yolo=yolo, cwd=chosen_cwd,
            executable=executable, fork=fork, permission_mode=permission_mode,
            model=model, prompt=prompt)
    return {
        "provider": session.provider,
        "session_id": session.session_id,
        "cwd": chosen_cwd,
        "command": command,
        "command_text": shlex.join(command),
        "detached": detached,
        "tmux_name": chosen_name,
        "tmux_command": tmux_command,
    }


def start_detached(
    session: Session,
    *,
    taken=None,
    yolo: bool = False,
    name: str = "",
    cwd: str = "",
    executable: str = "",
    force_live: bool = False,
    fork: bool = False,
    permission_mode: str = "",
    model: str = "",
    prompt: str = "",
    pacer=None,
    on_wait=None,
    runner=subprocess.run,
) -> str:
    """Start one session in a detached tmux session and return its name."""
    _check_live(session, force_live)
    check_installed(session, executable)
    working_directory(session, cwd)
    existing = tmux_sessions(runner=runner) if taken is None else set(taken)
    chosen_name = (requested_tmux_name(session, name, existing)
                   if name else tmux_name(session, existing))
    argv = tmux_argv(
        session, chosen_name, yolo=yolo, cwd=cwd, executable=executable,
        fork=fork, permission_mode=permission_mode, model=model, prompt=prompt)

    def create():
        try:
            result = runner(argv, capture_output=True, text=True, timeout=30,
                            check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"tmux could not start the session: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or "tmux failed"
            raise RuntimeError(detail)

    if pacer is None:
        create()
    else:
        with pacer.slot(session.session_id, on_wait=on_wait):
            create()
    return chosen_name


def attach(name: str) -> None:
    """Replace this process with a tmux attachment. Does not return."""
    tmux = config.configured_program("tmux", "tmux")
    os.execvp(tmux, [tmux, "attach-session", "-t", name])


def restore_all(sessions, *, gap: float = LAUNCH_GAP, yolo: bool = False,
                runner=subprocess.run, sleeper=time.sleep,
                on_wait=None, pacer=None) -> list[dict[str, object]]:
    """Start several sessions, waiting `gap` seconds between each launch.

    The wait happens *before* each launch after the first, and only when the
    previous one actually started — a failed launch consumed no quota, so the
    next one should not be made to pay for it.
    """
    results: list[dict[str, object]] = []
    taken = tmux_sessions(runner=runner)
    started = 0
    for session in sessions:
        if pacer is None and started and gap > 0:
            remaining = gap
            while remaining > 0:
                if on_wait is not None:
                    on_wait(remaining, session)
                step = min(1.0, remaining)
                sleeper(step)
                remaining -= step
        try:
            if pacer is None:
                name = start_detached(session, taken=taken, yolo=yolo,
                                      runner=runner)
            else:
                callback = (
                    (lambda remaining, _interval, item=session:
                     on_wait(remaining, item))
                    if on_wait is not None else None)
                name = start_detached(
                    session, taken=taken, yolo=yolo, runner=runner,
                    pacer=pacer, on_wait=callback)
        except RuntimeError as error:
            results.append({"session": session, "ok": False, "detail": str(error)})
            continue
        taken.add(name)
        started += 1
        results.append({"session": session, "ok": True, "detail": name})
    return results
