"""Install and update the coding agents themselves.

A recovery tool is useless if the agent that owns the transcript is missing, so
this offers the install — but an install here is a pipe from the network into a
shell, which is the vendor's documented method and still the most consequential
thing this tool can do. Every path therefore states the exact command and the
page it came from, and nothing runs without an explicit yes.

Updates delegate to each agent's own updater rather than re-running the install
script, so the agent stays in charge of how it upgrades itself.
"""
from __future__ import annotations

import os
import subprocess

from . import config
from .providers import Provider


def installed(item: Provider) -> str:
    """Return the resolved path to the agent's command, or an empty string."""
    return config.resolve_program(item.key, item.command)


def version(item: Provider, *, runner=subprocess.run) -> str:
    """Return the agent's reported version, or an empty string if unavailable."""
    if not installed(item):
        return ""
    try:
        result = runner([installed(item), "--version"], capture_output=True,
                        text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (result.stdout or result.stderr or "").strip().splitlines()
    return first[0].strip() if first else ""


def install_plan(item: Provider) -> dict[str, str]:
    """Describe the install without running it."""
    return {
        "agent": item.label,
        "command": item.install_shell,
        "source": item.install_source,
        "shell": _shell(),
    }


def update_plan(item: Provider) -> dict[str, str]:
    return {"agent": item.label, "command": " ".join(item.update_argv)}


def _shell() -> str:
    return os.environ.get("SHELL") or "/bin/sh"


def run_install(item: Provider, *, runner=subprocess.run) -> int:
    """Run the vendor's documented install command. Confirm before calling."""
    try:
        result = runner([_shell(), "-c", item.install_shell], check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"could not run the installer: {error}") from error
    return int(result.returncode)


def run_update(item: Provider, *, runner=subprocess.run) -> int:
    """Run the agent's own updater."""
    if not installed(item):
        raise RuntimeError(f"{item.label} is not installed")
    try:
        argv = list(item.update_argv)
        argv[0] = installed(item)
        result = runner(argv, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"could not run the updater: {error}") from error
    return int(result.returncode)


def status(providers) -> list[dict[str, object]]:
    """Report which agents are present, for the TUI and the launcher installer."""
    rows: list[dict[str, object]] = []
    for item in providers:
        path = installed(item)
        rows.append({
            "key": item.key,
            "label": item.label,
            "command": config.configured_program(item.key, item.command),
            "installed": bool(path),
            "path": path,
        })
    return rows
