"""The three coding agents, as one list.

Install commands are the vendor's own script, downloaded and checked against
the sha256 pinned here on 2026-09-25, then executed as a file. Updates go
through the agent's own updater. The source URL is shown before anything runs.
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
    install_url: str
    install_sha256: str
    install_interpreter: str
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
        install_url="https://claude.ai/install.sh",
        install_sha256="3a68d3406cf674e17bed1733a4dcf37805e2e47d87417700007d7e1aa766a944",
        install_interpreter="bash",
        install_shell="bash https://claude.ai/install.sh (sha256 3a68d3406cf674e17bed1733a4dcf37805e2e47d87417700007d7e1aa766a944)",
        install_source="https://code.claude.com/docs/en/quickstart",
        update_argv=("claude", "update"),
        discover=claude.discover,
        resume_argv=claude.resume_argv,
    ),
    Provider(
        key="codex",
        label="Codex",
        command="codex",
        install_url="https://chatgpt.com/codex/install.sh",
        install_sha256="150e3cf675682efeaac115aa3747add3f27887896d04ce6d0b56478d8b428bf6",
        install_interpreter="sh",
        install_shell="sh https://chatgpt.com/codex/install.sh (sha256 150e3cf675682efeaac115aa3747add3f27887896d04ce6d0b56478d8b428bf6)",
        install_source="https://developers.openai.com/codex/cli/",
        update_argv=("codex", "update"),
        discover=codex.discover,
        resume_argv=codex.resume_argv,
    ),
    Provider(
        key="kimi",
        label="Kimi Code",
        command="kimi",
        install_url="https://code.kimi.com/kimi-code/install.sh",
        install_sha256="270a86f2d2304529b6d8a3783fca9534874ebaeecb6cfcc1aebcdb6ce20ae1d7",
        install_interpreter="bash",
        install_shell="bash https://code.kimi.com/kimi-code/install.sh (sha256 270a86f2d2304529b6d8a3783fca9534874ebaeecb6cfcc1aebcdb6ce20ae1d7)",
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
