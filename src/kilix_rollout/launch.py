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
import subprocess
import time

from .model import Session
from .providers import Provider, provider

#: Seconds between queued launches. Restoring a batch means several agents
#: begin consuming tokens against one account-wide limit.
LAUNCH_GAP = 30.0


def resume_command(session: Session, *, yolo: bool = False) -> list[str]:
    return provider(session.provider).resume_argv(session, yolo=yolo)


def working_directory(session: Session) -> str:
    """Return the directory to resume in, or raise if it is gone."""
    cwd = session.cwd or ""
    if not cwd:
        raise RuntimeError("this session has no recorded working directory")
    if not os.path.isdir(cwd):
        raise RuntimeError(f"working directory no longer exists: {cwd}")
    return cwd


def check_installed(session: Session) -> Provider:
    from . import manage

    item = provider(session.provider)
    if not manage.installed(item):
        raise RuntimeError(
            f"{item.label} is not installed; install it before resuming")
    return item


def hand_over(session: Session, *, yolo: bool = False) -> None:
    """Replace this process with the resumed agent. Does not return."""
    check_installed(session)
    cwd = working_directory(session)
    argv = resume_command(session, yolo=yolo)
    os.chdir(cwd)
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


def tmux_sessions(*, runner=subprocess.run) -> set[str]:
    try:
        result = runner(["tmux", "list-sessions", "-F", "#{session_name}"],
                        capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()          # usually just "no server running"
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def tmux_argv(session: Session, name: str, *, yolo: bool = False) -> list[str]:
    return ["tmux", "new-session", "-d", "-s", name, "-c",
            session.cwd, *resume_command(session, yolo=yolo)]


def start_detached(session: Session, *, taken=None, yolo: bool = False,
                   runner=subprocess.run) -> str:
    """Start one session in a detached tmux session and return its name."""
    check_installed(session)
    working_directory(session)
    existing = tmux_sessions(runner=runner) if taken is None else set(taken)
    name = tmux_name(session, existing)
    try:
        result = runner(tmux_argv(session, name, yolo=yolo), capture_output=True,
                        text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"tmux could not start the session: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "tmux failed"
        raise RuntimeError(detail)
    return name


def restore_all(sessions, *, gap: float = LAUNCH_GAP, yolo: bool = False,
                runner=subprocess.run, sleeper=time.sleep,
                on_wait=None) -> list[dict[str, object]]:
    """Start several sessions, waiting `gap` seconds between each launch.

    The wait happens *before* each launch after the first, and only when the
    previous one actually started — a failed launch consumed no quota, so the
    next one should not be made to pay for it.
    """
    results: list[dict[str, object]] = []
    taken = tmux_sessions(runner=runner)
    started = 0
    for session in sessions:
        if started and gap > 0:
            remaining = gap
            while remaining > 0:
                if on_wait is not None:
                    on_wait(remaining, session)
                step = min(1.0, remaining)
                sleeper(step)
                remaining -= step
        try:
            name = start_detached(session, taken=taken, yolo=yolo,
                                  runner=runner)
        except RuntimeError as error:
            results.append({"session": session, "ok": False, "detail": str(error)})
            continue
        taken.add(name)
        started += 1
        results.append({"session": session, "ok": True, "detail": name})
    return results
