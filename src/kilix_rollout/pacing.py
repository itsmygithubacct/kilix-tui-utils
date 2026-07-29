"""Serialize token-consuming launches across command invocations."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import config

MINIMUM_INTERVAL = config.DEFAULT_GAP


@dataclass(frozen=True)
class LaunchRecord:
    at: float
    session_id: str
    count: int


def read_launch_record(path: Path | None = None) -> LaunchRecord | None:
    source = (path or config.state_path()).expanduser()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        return LaunchRecord(
            at=float(raw["at"]),
            session_id=str(raw.get("session_id") or ""),
            count=int(raw.get("count") or 0),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_launch_record(record: LaunchRecord, path: Path) -> None:
    existed = path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({
                "at": record.at,
                "session_id": record.session_id,
                "count": record.count,
            }, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class LaunchPacer:
    """Guarantee a minimum gap, including between concurrent processes."""

    def __init__(
        self,
        *,
        interval: float = MINIMUM_INTERVAL,
        state_file: Path | None = None,
        lock_file: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if interval < MINIMUM_INTERVAL:
            raise RuntimeError(
                f"launch gap must be at least {MINIMUM_INTERVAL:.0f} seconds")
        self.interval = float(interval)
        self.state_file = (state_file or config.state_path()).expanduser()
        self.lock_file = (lock_file or config.lock_path()).expanduser()
        self.clock = clock
        self.sleeper = sleeper

    def remaining(self, *, now: float | None = None) -> float:
        record = read_launch_record(self.state_file)
        if record is None:
            return 0.0
        elapsed = (self.clock() if now is None else now) - record.at
        if elapsed < 0:
            return self.interval
        return max(0.0, self.interval - elapsed)

    def _wait(self, on_wait) -> float:
        waited = 0.0
        while True:
            remaining = self.remaining()
            if remaining <= 0:
                return waited
            if on_wait is not None and on_wait(remaining, self.interval) is False:
                raise KeyboardInterrupt
            step = min(0.5, remaining)
            self.sleeper(step)
            waited += step

    @contextmanager
    def slot(self, session_id: str = "", *, on_wait=None) -> Iterator[float]:
        """Wait while locked, then stamp only after a successful launch."""
        with self._locked():
            waited = self._wait(on_wait)
            yield waited
            previous = read_launch_record(self.state_file)
            _write_launch_record(
                LaunchRecord(
                    at=self.clock(),
                    session_id=session_id,
                    count=(previous.count if previous else 0) + 1,
                ),
                self.state_file,
            )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        existed = self.lock_file.parent.exists()
        self.lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not existed:
            self.lock_file.parent.chmod(0o700)
        try:
            handle = self.lock_file.open("a+")
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                raise
            yield
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
