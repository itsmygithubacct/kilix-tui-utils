"""Optional bridge from every Kilix utility to the shared telemetry ring."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

_PACKAGE: ModuleType | bool | None = None
_CLIENT: Any | None = None
_NEXT_START_ATTEMPT = 0.0


def _source_directory(value: str | os.PathLike[str]) -> Path | None:
    candidate = Path(value).expanduser().resolve()
    for source in (candidate, candidate / "src"):
        if (source / "kilix_telemetry" / "__init__.py").is_file():
            return source
    return None


def _candidate_sources() -> tuple[Path, ...]:
    values: list[str | os.PathLike[str]] = []
    if explicit := os.environ.get("KILIX_TELEMETRY_SOURCE"):
        values.append(explicit)
    if kilix_home := os.environ.get("KILIX_HOME"):
        values.append(Path(kilix_home) / "third_party" / "kilix-telemetry")
    if source_home := os.environ.get("GPU_TERMINAL_SOURCE_HOME"):
        values.append(Path(source_home) / "kilix-modules" / "kilix-telemetry")
    found: list[Path] = []
    for value in values:
        try:
            source = _source_directory(value)
        except OSError:
            source = None
        if source is not None and source not in found:
            found.append(source)
    return tuple(found)


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _load_from(source: Path) -> ModuleType | None:
    name = "kilix_telemetry"
    existing = sys.modules.get(name)
    if isinstance(existing, ModuleType):
        location = getattr(existing, "__file__", "")
        if location and _is_below(Path(location), source):
            return existing
        return None
    package = source / name
    spec = importlib.util.spec_from_file_location(
        name,
        package / "__init__.py",
        submodule_search_locations=[str(package)],
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        return None
    return module


def _compatible(module: ModuleType) -> bool:
    try:
        major, minor = module.TELEMETRY_API_VERSION
        return int(major) == 1 and int(minor) >= 1
    except (AttributeError, TypeError, ValueError):
        return False


def _package() -> ModuleType | None:
    global _PACKAGE
    if isinstance(_PACKAGE, ModuleType):
        return _PACKAGE
    if _PACKAGE is False:
        return None
    sources = _candidate_sources()
    for source in sources:
        if (module := _load_from(source)) is not None and _compatible(module):
            _PACKAGE = module
            return module
    if not sources:
        try:
            module = importlib.import_module("kilix_telemetry")
        except Exception:
            module = None
        if isinstance(module, ModuleType) and _compatible(module):
            _PACKAGE = module
            return module
    _PACKAGE = False
    return None


def _client() -> Any | None:
    global _CLIENT
    if _CLIENT is None and (package := _package()) is not None:
        try:
            _CLIENT = package.TelemetryClient(cache_seconds=0.0)
        except Exception:
            return None
    return _CLIENT


def snapshot(*, force: bool = True) -> Any | None:
    """Return one shared record without waiting for sampler readiness."""
    global _NEXT_START_ATTEMPT
    client = _client()
    if client is None:
        return None
    now = time.monotonic()
    start = now >= _NEXT_START_ATTEMPT
    if start:
        _NEXT_START_ATTEMPT = now + 30.0
    try:
        result = client.snapshot(start=False, fallback=False, force=force)
    except Exception:
        result = None
    if start and (package := _package()) is not None:
        try:
            package.ensure_running(client.paths, timeout=0.0)
        except Exception:
            pass
    return result


__all__ = ["snapshot"]
