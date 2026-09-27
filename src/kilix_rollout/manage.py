"""Install and update the coding agents themselves.

A recovery tool is useless if the agent that owns the transcript is missing, so
this offers the install. The vendor script is downloaded, checked against the
sha256 on the provider, and only then executed as a file. Every path states
the URL, the pin and the source page, and nothing runs without an explicit yes.

Updates delegate to each agent's own updater rather than re-running the install
script, so the agent stays in charge of how it upgrades itself.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

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


def fetch_pinned(url: str, digest: str, *, timeout: float = 60) -> bytes:
    """Download an installer and refuse it unless it matches digest."""
    expected = digest.lower()
    request = urllib.request.Request(url, headers={"User-Agent": "kilix-install"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(f"could not download {url}: {error}") from error
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"installer from {url} sha256 {actual} does not match pin {expected}")
    return payload


def run_install(item: Provider, *, runner=subprocess.run) -> int:
    """Run the pinned vendor script. Confirm before calling."""
    if item.install_interpreter not in {"bash", "sh"}:
        raise RuntimeError(f"{item.label}: installer interpreter is not bash or sh")
    payload = fetch_pinned(item.install_url, item.install_sha256)
    handle = tempfile.NamedTemporaryFile(
        prefix=f"kilix-{item.key}-install-", suffix=".sh", delete=False)
    path = handle.name
    try:
        handle.write(payload)
        handle.close()
        os.chmod(path, 0o700)
        try:
            result = runner([item.install_interpreter, path], check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"could not run the installer: {error}") from error
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return int(result.returncode)


def post_install_report(item: Provider) -> str:
    """One honest line about where the install actually landed.

    An installer's zero exit says nothing about reachability: claude lands
    in ~/.local/bin (which Debian only puts on PATH for login shells), kimi
    under ~/.kimi-code/bin. Resolve again and say what happened, so a
    PATH-invisible install never reads as a plain success.
    """
    path = installed(item)
    if not path:
        return (f"{item.label}: the installer finished but "
                f"`{item.command}` does not resolve — check its output")
    on_path = bool(shutil.which(
        config.configured_program(item.key, item.command)))
    if on_path:
        return f"{item.label} installed at {path}"
    return (f"{item.label} installed at {path} — not on this shell's "
            "PATH; new kilix panes resolve it, current shells need "
            f"{os.path.dirname(path)} added")


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
