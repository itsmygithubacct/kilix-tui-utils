"""Configuration shared by the rollout-resume CLI and picker.

The dangerous-permissions default remains a stack-wide Kilix setting.  Paths
and launch pacing are local details of this tool, so they live in a small
private JSON file.  Legacy Claude/Codex standalone settings are read as
fallbacks during the migration; writing always targets the unified file.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from kilix_tui import theme

#: Shared settings key, declared by kilix_sdk.settings.
YOLO_KEY = "KILIX_CODING_YOLO"

DEFAULT_GAP = 30.0
_TRUE = frozenset({"on", "1", "true", "yes"})
_PROGRAM_KEYS = frozenset({"tmux", "claude", "codex", "kimi"})
_NUMBER_KEYS = frozenset({"gap"})
_CONFIG_KEYS = _PROGRAM_KEYS | _NUMBER_KEYS


def yolo_default() -> bool:
    """Return whether resumed agents should skip their approval prompts."""
    return str(theme.setting(YOLO_KEY, "off")).strip().casefold() in _TRUE


def yolo_flag(provider_key: str) -> str:
    """Return the flag that agent uses, for showing the user what will run."""
    return ("--dangerously-skip-permissions" if provider_key == "claude"
            else "--yolo")


def app_home() -> Path:
    configured = os.environ.get("KILIX_ROLLOUT_RESUME_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "kilix_rollout_resume"


def config_path() -> Path:
    return app_home() / "config.json"


def state_path() -> Path:
    return app_home() / "launches.json"


def lock_path() -> Path:
    return app_home() / "launches.lock"


def _legacy_paths() -> tuple[Path, Path]:
    claude_home = os.environ.get("CLAUDE_ROLLOUT_RESUME_HOME")
    codex_home = os.environ.get("CODEX_ROLLOUT_RESUME_HOME")
    return (
        (Path(claude_home).expanduser() if claude_home
         else Path.home() / ".local" / "claude_rollout_resume") / "config.json",
        (Path(codex_home).expanduser() if codex_home
         else Path.home() / ".local" / "codex_rollout_resume") / "config.json",
    )


def _read(
    path: Path,
    *,
    strict: bool,
    allow_legacy_interval: bool = False,
) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        if strict:
            raise RuntimeError(f"cannot read configuration {path}: {error}") from error
        return {}
    if not isinstance(raw, dict):
        if strict:
            raise RuntimeError(f"configuration must be a JSON object: {path}")
        return {}

    settings: dict[str, object] = {}
    for key, value in raw.items():
        if key in _PROGRAM_KEYS and isinstance(value, str) and value.strip():
            settings[key] = value
        elif key in _NUMBER_KEYS and isinstance(value, (int, float)):
            settings[key] = float(value)
        elif (allow_legacy_interval and key == "interval"
              and isinstance(value, (int, float))):
            # Name used by the retired Claude-only tool.
            settings[key] = float(value)
    return settings


def load_config(path: Path | None = None) -> dict[str, object]:
    """Load validated settings.

    With the default path, useful settings from the retired provider-specific
    tools are inherited until the user writes a unified configuration.
    ``tb`` is deliberately not inherited: it is a different command-line
    interface, while the unified tool talks directly to tmux.
    """
    if path is not None:
        return _read(path.expanduser(), strict=True)

    settings: dict[str, object] = {}
    claude_legacy, codex_legacy = _legacy_paths()
    claude_settings = _read(
        claude_legacy, strict=False, allow_legacy_interval=True)
    codex_settings = _read(codex_legacy, strict=False)
    for key in ("claude",):
        if key in claude_settings:
            settings[key] = claude_settings[key]
    for key in ("codex",):
        if key in codex_settings:
            settings[key] = codex_settings[key]
    interval = claude_settings.get("interval")
    if isinstance(interval, (int, float)):
        settings["gap"] = float(interval)
    settings.update(_read(config_path(), strict=True))
    return settings


def write_config(
    updates: Mapping[str, str | float | Path | None],
    *,
    path: Path | None = None,
) -> Path:
    """Merge settings and atomically write a mode-0600 configuration file."""
    destination = (path or config_path()).expanduser()
    unknown = set(updates) - _CONFIG_KEYS
    if unknown:
        raise RuntimeError(
            "unknown configuration key(s): " + ", ".join(sorted(unknown)))

    # An explicit path is important here: legacy fallbacks should not be
    # materialized merely because one setting is changed.
    settings = load_config(destination)
    for key, value in updates.items():
        if value is None:
            settings.pop(key, None)
        elif key in _NUMBER_KEYS:
            settings[key] = float(value)
        else:
            text = str(value).strip()
            if not text:
                settings.pop(key, None)
            else:
                candidate = Path(text).expanduser()
                settings[key] = (str(candidate.absolute())
                                 if candidate.is_file() else text)

    existed = destination.parent.exists()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        destination.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def configured_program(key: str, default: str) -> str:
    """Return the configured command or its ordinary PATH name."""
    if key not in _PROGRAM_KEYS:
        raise KeyError(key)
    environment = os.environ.get(
        f"KILIX_ROLLOUT_RESUME_{key.upper()}")
    if environment:
        return environment
    legacy_environment = os.environ.get(
        f"{key.upper()}_ROLLOUT_RESUME_{key.upper()}")
    if legacy_environment:
        return legacy_environment
    value = load_config().get(key)
    return str(value) if isinstance(value, str) and value else default


def resolve_program(key: str, default: str) -> str:
    """Resolve a configured program to an executable path, or return ``""``."""
    value = configured_program(key, default)
    candidate = Path(value).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate.absolute())
    return shutil.which(value) or ""


def launch_gap() -> float:
    raw = os.environ.get("KILIX_ROLLOUT_RESUME_GAP")
    if raw is None:
        raw = load_config().get("gap", DEFAULT_GAP)
    try:
        gap = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GAP
    return max(0.0, gap)
