"""Read settings from the one shared Kilix configuration file.

Every Kilix component — the terminal, both desktop providers, Pleb, and
Plebian-OS — already reads `~/.local/gpu_terminal/settings.conf`. These tools
join that contract rather than inventing a second place to configure them, so a
change made in any settings interface reaches all of them.

The SDK is imported when a Kilix checkout is reachable and its absence is not
fatal: a tool run from a bare checkout, over SSH, or on a machine without Kilix
installed still starts with the built-in defaults.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sys
from typing import Any

_SDK: Any | None = None


def _load_settings(path: Path) -> Any:
    """Load one selected settings file without altering global import paths."""
    resolved = path.resolve()
    digest = hashlib.sha256(os.fsencode(resolved)).hexdigest()[:16]
    module_name = f"_kilix_tui_host_settings_{digest}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        origin = Path(getattr(existing, "__file__", "")).resolve()
        if origin == resolved:
            return existing
        raise ImportError(f"{module_name} is already loaded from {origin}")
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Kilix settings from {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _sdk() -> Any | None:
    """Return `kilix_sdk.settings`, or None when Kilix is not reachable."""
    global _SDK
    if _SDK is not None:
        return _SDK or None
    source_home = os.environ.get("GPU_TERMINAL_SOURCE_HOME") or os.path.join(
        os.path.expanduser("~"), "gpu_terminal")
    candidates = [
        os.environ.get("KILIX_HOME", ""),
        os.path.join(os.path.abspath(os.path.expanduser(source_home)), "kilix"),
    ]
    for home in candidates:
        settings = (
            Path(home) / "config" / "kilix_sdk" / "settings.py"
            if home else None
        )
        if settings is not None and settings.is_file():
            try:
                _SDK = _load_settings(settings)
                return _SDK
            except Exception:
                break
    _SDK = False  # type: ignore[assignment]
    return None


def sdk_settings() -> Any | None:
    """Return the selected host settings module for shared desktop callers."""
    return _sdk()


def setting(key: str, default: str) -> str:
    """Read one shared setting, falling back when Kilix is unavailable."""
    sdk = _sdk()
    if sdk is None:
        return os.environ.get(key, default)
    try:
        return sdk.load().get(key, default)
    except Exception:
        return default
