"""The three coding agents, as one list.

Install commands are quoted from each vendor's own documentation rather than
invented here, and updates go through the agent's own updater so it stays the
authority on how it upgrades itself. The `source` URL is shown next to the
command before anything runs, so an install is never an opaque pipe to a shell.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import claude, codex, kimi
from .model import Session, newest_first


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    command: str
    install_shell: str
    install_source: str
    update_argv: tuple[str, ...]
    discover: Callable[..., list[Session]]
    resume_argv: Callable[[Session], list[str]]


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="claude",
        label="Claude Code",
        command="claude",
        install_shell="curl -fsSL https://claude.ai/install.sh | bash",
        install_source="https://code.claude.com/docs/en/quickstart",
        update_argv=("claude", "update"),
        discover=claude.discover,
        resume_argv=claude.resume_argv,
    ),
    Provider(
        key="codex",
        label="Codex",
        command="codex",
        install_shell="curl -fsSL https://chatgpt.com/codex/install.sh | sh",
        install_source="https://developers.openai.com/codex/cli/",
        update_argv=("codex", "update"),
        discover=codex.discover,
        resume_argv=codex.resume_argv,
    ),
    Provider(
        key="kimi",
        label="Kimi Code",
        command="kimi",
        install_shell="curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash",
        install_source="https://moonshotai.github.io/kimi-code/",
        update_argv=("kimi", "upgrade"),
        discover=kimi.discover,
        resume_argv=kimi.resume_argv,
    ),
)

_BY_KEY = {item.key: item for item in PROVIDERS}


def provider(key: str) -> Provider:
    try:
        return _BY_KEY[key]
    except KeyError:
        known = ", ".join(_BY_KEY)
        raise KeyError(f"unknown agent '{key}'; use one of: {known}") from None


def discover(
    keys=None,
    *,
    since: float = 0.0,
    roots: dict[str, str] | None = None,
    include_archived: bool = False,
    include_orphans: bool = True,
    proc_root: str = "/proc",
    selectors: tuple[str, ...] = (),
) -> list[Session]:
    """Collect sessions from every selected agent, newest first.

    One agent being absent or mid-write must not hide the others, so a failing
    provider contributes nothing rather than raising.
    """
    chosen = PROVIDERS if keys is None else [provider(key) for key in keys]
    roots = roots or {}
    found: list[Session] = []
    for item in chosen:
        try:
            options = {
                "root": roots.get(item.key, ""),
                "proc_root": proc_root,
                "since": since,
                "selectors": selectors,
            }
            if item.key == "claude":
                projects = roots.get("claude_projects", "")
                if projects:
                    options["projects_root"] = projects
                options["include_orphans"] = include_orphans
            elif item.key == "codex":
                sessions = roots.get("codex_sessions", "")
                if sessions:
                    options["sessions_root"] = sessions
                options["include_archived"] = include_archived
            found.extend(item.discover(**options))
        except OSError:
            continue
    return newest_first(found)
