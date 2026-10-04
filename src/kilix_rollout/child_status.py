"""Private startup and exit receipts for a detached resumed process.

A started receipt means the child process was created, not that the agent
is ready or has completed its task.
No command arguments, prompts, environment values or terminal text are stored.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def write(path: Path, **data) -> None:
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(dict(at=time.time(), **data), stream)
    temporary.replace(path)


def wait_started(path: Path, *, timeout: float = 5.0, settle: float = 0.25) -> dict:
    deadline = time.monotonic() + timeout
    observed = None
    while time.monotonic() < deadline:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as error:
            raise RuntimeError(f'cannot read child startup status: {path}') from error
        stage = data.get('stage')
        if stage == 'failed' or (stage == 'exited' and data.get('exit') != 0):
            raise RuntimeError(f'resumed child failed during startup (exit {data.get("exit", "unknown")}); status: {path}')
        if stage == 'exited':
            return data
        if stage == 'started':
            if observed is None:
                observed = time.monotonic()
            if time.monotonic() - observed >= settle:
                return data
        time.sleep(min(0.025, max(0, deadline-time.monotonic())))
    raise RuntimeError(f'no child startup acknowledgment within {timeout:g}s; status: {path}')


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != '--':
        raise SystemExit('usage: child_status.py STATUS -- PROGRAM [ARGS...]')
    path = Path(args[0])
    return _run(path, args[2:])


def _run(path: Path, command: list[str]) -> int:
    try:
        process = subprocess.Popen(command)
    except OSError as error:
        write(path, stage='failed', exit=127, error=type(error).__name__)
        return 127
    write(path, stage='started', pid=process.pid)
    while True:
        try:
            code = process.wait()
            break
        except KeyboardInterrupt:
            # Terminal signals reach the child's foreground process group too.
            continue
    write(path, stage='exited', pid=process.pid, exit=code)
    return code if code >= 0 else 128-code


if __name__ == '__main__':
    sys.exit(main())
