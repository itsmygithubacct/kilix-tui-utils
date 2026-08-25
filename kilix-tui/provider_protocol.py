"""Dependency-free protocol-v1 adapter for the Kilix TUI desktop."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from typing import Any


PROVIDER_ID = "kilix-tui"
CONTRACT_VERSION = 1
EXIT_USAGE = 2
EXIT_INVALID_REQUEST = 3
EXIT_UNAVAILABLE = 4
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _version() -> str:
    try:
        with open(os.path.join(_ROOT, "VERSION"), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return "0.0.0-unknown"


def _unavailable(reason: str, detail: str) -> dict[str, object]:
    return {"available": False, "detail": detail, "reason": reason}


def _emit(document: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _description() -> dict[str, Any]:
    return {
        "capabilities": {
            "audio": _unavailable(
                "not-implemented", "The desktop shell does not own audio output."
            ),
            "headless_screenshot": True,
            "keyboard": True,
            "launcher": True,
            "mouse": True,
            "reduced_motion": True,
            "settings": _unavailable(
                "not-implemented",
                "Protocol configuration writes are not available in this adapter.",
            ),
        },
        "config_schema": "kilix.desktop.config.provider.v1",
        "contract_version": CONTRACT_VERSION,
        "display_modes": ["terminal-text", "kitty-graphics"],
        "provider_id": PROVIDER_ID,
        "provider_version": _version(),
        "required_capabilities": ["keyboard"],
        "schema_version": 1,
    }


def _check() -> dict[str, Any]:
    tui_ready = all(
        os.path.isfile(os.path.join(_ROOT, "src", "kilix_tui", name))
        for name in ("__init__.py", "app.py", "shell.py")
    )
    desk_ready = all(
        os.path.isfile(os.path.join(_ROOT, "src", "kilix_desk", name))
        for name in ("__init__.py", "desk.py")
    )
    curses_ready = importlib.util.find_spec("curses") is not None
    checks = [
        {
            "id": "kilix-tui",
            "required": True,
            "status": "pass" if tui_ready else "unavailable",
            "summary": (
                "The kilix_tui package is readable."
                if tui_ready
                else "The kilix_tui package is missing."
            ),
        },
        {
            "id": "kilix-desk",
            "required": True,
            "status": "pass" if desk_ready else "unavailable",
            "summary": (
                "The kilix_desk package is readable."
                if desk_ready
                else "The kilix_desk package is missing."
            ),
        },
        {
            "id": "curses",
            "required": True,
            "status": "pass" if curses_ready else "unavailable",
            "summary": (
                "Python curses support is importable."
                if curses_ready
                else "Python curses support is not importable."
            ),
            **(
                {}
                if curses_ready
                else {"remediation": "Install Python with curses support."}
            ),
        },
    ]
    ready = tui_ready and desk_ready and curses_ready
    return {
        "checks": checks,
        "contract_version": CONTRACT_VERSION,
        "provider_id": PROVIDER_ID,
        "schema_version": 1,
        "status": "ready" if ready else "unavailable",
        "summary": (
            "Kilix TUI is ready."
            if ready
            else "Kilix TUI is missing a required runtime dependency."
        ),
    }


def _config_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.kilix.org/desktop/config/kilix-tui/v1",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {},
        "type": "object",
        "x-kilix-contract-version": CONTRACT_VERSION,
        "x-kilix-provider-id": PROVIDER_ID,
    }


def _config_values() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "provider_id": PROVIDER_ID,
        "revision": 0,
        "schema_version": 1,
        "values": {},
    }


def _fail(message: str, status: int) -> None:
    print(f"kilix-tui: {message}", file=sys.stderr)
    raise SystemExit(status)


def dispatch(argv: list[str]) -> list[str] | None:
    """Handle protocol commands or translate launch/screenshot argv."""
    if argv == ["--version"]:
        print(f"kilix-tui {_version()}")
        raise SystemExit(0)
    if not argv or argv[0] != "provider":
        return None

    if argv == ["provider", "describe", "--json"]:
        _emit(_description())
        raise SystemExit(0)
    if argv == ["provider", "check", "--json"]:
        _emit(_check())
        raise SystemExit(0)
    if argv == ["provider", "config", "schema", "--json"]:
        _emit(_config_schema())
        raise SystemExit(0)
    if argv == ["provider", "config", "get", "--json"] or (
        len(argv) == 5
        and argv[:3] == ["provider", "config", "get"]
        and argv[4] == "--json"
        and bool(argv[3])
    ):
        _emit(_config_values())
        raise SystemExit(0)
    if len(argv) == 5 and argv[:3] == ["provider", "config", "set"]:
        _fail("protocol configuration writes are unavailable", EXIT_UNAVAILABLE)

    if argv[:2] == ["provider", "launch"]:
        if len(argv) == 2:
            return []
        if (
            len(argv) == 4
            and argv[2] == "--session-id"
            and _SESSION_ID.fullmatch(argv[3]) is not None
        ):
            os.environ["KILIX_DESKTOP_SESSION_ID"] = argv[3]
            return []
        if len(argv) == 4 and argv[2] == "--session-id":
            _fail("invalid provider session ID", EXIT_INVALID_REQUEST)
        _fail("usage: provider launch [--session-id ID]", EXIT_USAGE)

    if argv[:2] == ["provider", "screenshot"]:
        if len(argv) < 3 or not argv[2]:
            _fail("usage: provider screenshot OUTPUT [OPTIONS...]", EXIT_USAGE)
        return ["--screenshot", argv[2], *argv[3:]]

    if argv[:2] == ["provider", "migrate"]:
        if (
            len(argv) not in (4, 5)
            or argv[2] != "--from"
            or not argv[3]
            or len(argv[3]) > 64
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in argv[3])
            or (len(argv) == 5 and argv[4] != "--dry-run")
        ):
            _fail("usage: provider migrate --from VERSION [--dry-run]", EXIT_USAGE)
        _fail("protocol persistence migration is unavailable", EXIT_UNAVAILABLE)

    _fail("unknown provider protocol command", EXIT_USAGE)
    raise AssertionError("unreachable")
